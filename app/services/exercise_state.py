"""Rebuild UserExerciseState from remaining finished history (AI/delete consistency)."""

from __future__ import annotations

import json
from collections import defaultdict
from collections.abc import Iterable
from datetime import date, timedelta
from typing import Any

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.db.models import (
    Difficulty,
    ExerciseNoteLog,
    SessionSet,
    SessionStatus,
    TemplateExercise,
    User,
    UserExerciseState,
    UserExerciseWeekPlan,
    WorkoutSession,
)
from app.services.progression import suggest_next_weight


def _unique_set_count(sets: list[SessionSet]) -> int:
    nums = {int(s.set_number or 0) for s in sets if (s.set_number or 0) > 0}
    if nums:
        return len(nums)
    return max((int(s.sets_count or 1) for s in sets), default=0) or len(sets)


def _work_set(sets: list[SessionSet]) -> SessionSet:
    mains = [s for s in sets if int(s.drop_index or 0) == 0]
    pool = mains or sets
    return sorted(
        pool,
        key=lambda s: (int(s.set_number or 0), int(s.drop_index or 0), s.id or 0),
    )[-1]


def _target_week_start(today: date | None = None) -> date:
    today = today or date.today()
    this_monday = today - timedelta(days=today.weekday())
    if today.weekday() >= 5:
        return this_monday + timedelta(days=7)
    return this_monday


def _first_plan_kg(raw: str | None) -> float | None:
    if not raw:
        return None
    try:
        data = json.loads(raw)
    except (TypeError, json.JSONDecodeError):
        return None
    if not isinstance(data, list) or not data:
        return None
    row = data[0]
    if not isinstance(row, dict):
        return None
    try:
        kg = float(row.get("kg") or row.get("weight") or 0)
    except (TypeError, ValueError):
        return None
    return kg if kg > 0 else None


async def _week_plan_suggested_kg(
    session: AsyncSession, user_id: int, exercise_id: int
) -> float | None:
    week_start = _target_week_start()
    plan = (
        await session.execute(
            select(UserExerciseWeekPlan).where(
                UserExerciseWeekPlan.user_id == user_id,
                UserExerciseWeekPlan.exercise_id == exercise_id,
                UserExerciseWeekPlan.week_start == week_start,
            )
        )
    ).scalar_one_or_none()
    if not plan:
        return None
    return _first_plan_kg(plan.sets_json)


async def rebuild_user_exercise_states(
    session: AsyncSession,
    user_id: int,
    exercise_ids: Iterable[int | None],
) -> None:
    """Restore working/suggested weights from remaining finished sets.

    Call after deleting sessions/sets so AI and auto-weights match history.
    Does not commit — caller owns the transaction.
    """
    ids = sorted({int(x) for x in exercise_ids if x is not None})
    if not ids:
        return

    user = await session.get(User, user_id)
    if not user:
        return

    for exercise_id in ids:
        rows = list(
            (
                await session.execute(
                    select(SessionSet)
                    .join(WorkoutSession, SessionSet.session_id == WorkoutSession.id)
                    .where(
                        WorkoutSession.user_id == user_id,
                        WorkoutSession.status == SessionStatus.finished,
                        SessionSet.exercise_id == exercise_id,
                    )
                    .options(selectinload(SessionSet.session))
                    .order_by(
                        WorkoutSession.session_date.asc(),
                        WorkoutSession.id.asc(),
                        SessionSet.set_number.asc(),
                        SessionSet.drop_index.asc(),
                        SessionSet.id.asc(),
                    )
                )
            )
            .scalars()
            .all()
        )

        st = (
            await session.execute(
                select(UserExerciseState).where(
                    UserExerciseState.user_id == user_id,
                    UserExerciseState.exercise_id == exercise_id,
                )
            )
        ).scalar_one_or_none()

        plan_kg = await _week_plan_suggested_kg(session, user_id, exercise_id)

        if not rows:
            if plan_kg is not None:
                if st is None:
                    st = UserExerciseState(
                        user_id=user_id,
                        exercise_id=exercise_id,
                        suggested_weight=plan_kg,
                    )
                    session.add(st)
                else:
                    st.working_weight = None
                    st.suggested_weight = plan_kg
                    st.last_reps = None
                    st.last_sets = None
                    st.last_difficulty = None
                    st.hard_streak = 0
                    # keep note — independent of lift history
            elif st is not None:
                await session.delete(st)
            continue

        by_session: dict[int, list[SessionSet]] = defaultdict(list)
        session_order: list[int] = []
        for s in rows:
            sid = int(s.session_id)
            if sid not in by_session:
                session_order.append(sid)
            by_session[sid].append(s)

        hard_streak = 0
        last_group: list[SessionSet] = []
        for sid in session_order:
            group = by_session[sid]
            last_group = group
            diff = group[-1].difficulty
            if diff in (Difficulty.hard, Difficulty.failure):
                hard_streak += 1
            else:
                hard_streak = 0

        work = _work_set(last_group)
        weight = float(work.weight)
        reps = int(work.reps)
        sets_count = _unique_set_count(last_group)
        difficulty = work.difficulty

        exercise = await session.get(TemplateExercise, exercise_id)
        suggested = weight
        if exercise is not None:
            # Replay suggest from empty streak baseline for this last event:
            # hard_streak on state before this event = current-1 if hard else 0.
            prior_streak = hard_streak - 1 if difficulty in (
                Difficulty.hard,
                Difficulty.failure,
            ) else 0
            prior = UserExerciseState(
                user_id=user_id,
                exercise_id=exercise_id,
                hard_streak=max(0, prior_streak),
            )
            suggested, _ = suggest_next_weight(
                phase=user.phase,
                exercise=exercise,
                weight=weight,
                reps=reps,
                sets_count=sets_count,
                difficulty=difficulty,
                state=prior,
            )
        if plan_kg is not None:
            suggested = plan_kg

        if st is None:
            st = UserExerciseState(user_id=user_id, exercise_id=exercise_id)
            session.add(st)
        st.working_weight = weight
        st.suggested_weight = suggested
        st.last_reps = reps if reps > 0 else None
        st.last_sets = sets_count
        st.last_difficulty = difficulty
        st.hard_streak = hard_streak


async def exercise_ids_from_sets(sets: Iterable[Any]) -> list[int]:
    out: list[int] = []
    seen: set[int] = set()
    for s in sets:
        eid = getattr(s, "exercise_id", None)
        if eid is None:
            continue
        eid = int(eid)
        if eid in seen:
            continue
        seen.add(eid)
        out.append(eid)
    return out


async def scrub_skipped_session_orphans(session: AsyncSession) -> dict[str, int]:
    """Hard-delete legacy soft-skipped sessions and their notes (AI leak cleanup)."""
    skipped_ids = list(
        (
            await session.execute(
                select(WorkoutSession.id).where(
                    WorkoutSession.status == SessionStatus.skipped
                )
            )
        )
        .scalars()
        .all()
    )
    if not skipped_ids:
        return {"sessions": 0, "notes": 0}

    # Collect exercise ids before wipe for state rebuild per user
    rows = list(
        (
            await session.execute(
                select(WorkoutSession)
                .where(WorkoutSession.id.in_(skipped_ids))
                .options(selectinload(WorkoutSession.sets))
            )
        )
        .scalars()
        .all()
    )
    by_user: dict[int, set[int]] = defaultdict(set)
    for ws in rows:
        for eid in await exercise_ids_from_sets(ws.sets or []):
            by_user[int(ws.user_id)].add(eid)

    notes = await session.execute(
        delete(ExerciseNoteLog).where(ExerciseNoteLog.session_id.in_(skipped_ids))
    )
    sessions = await session.execute(
        delete(WorkoutSession).where(WorkoutSession.id.in_(skipped_ids))
    )
    await session.flush()
    for uid, eids in by_user.items():
        await rebuild_user_exercise_states(session, uid, eids)
    await session.commit()
    return {
        "sessions": int(sessions.rowcount or 0),
        "notes": int(notes.rowcount or 0),
    }
