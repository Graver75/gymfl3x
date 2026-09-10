"""Build an athlete JSON snapshot for future NN feedback."""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.db.models import (
    BodyWeightLog,
    ExerciseNoteLog,
    SessionStatus,
    User,
    UserExerciseState,
    WorkoutSession,
)
from app.services.reminders import get_template_for_weekday
from app.services.users import effective_experience_months


def _iso(dt: datetime | date | None) -> str | None:
    if dt is None:
        return None
    if isinstance(dt, datetime):
        return dt.isoformat()
    return dt.isoformat()


def _rest_seconds(prev: datetime | None, cur: datetime | None) -> int | None:
    if not prev or not cur:
        return None
    if prev.tzinfo is None:
        prev = prev.replace(tzinfo=timezone.utc)
    if cur.tzinfo is None:
        cur = cur.replace(tzinfo=timezone.utc)
    return max(0, int((cur - prev).total_seconds()))


async def build_athlete_snapshot(
    session: AsyncSession,
    user_id: int,
    *,
    days: int = 60,
) -> dict[str, Any]:
    user = await session.get(User, user_id)
    if not user:
        return {"error": "user_not_found", "user_id": user_id}

    since = date.today() - timedelta(days=days - 1)
    result = await session.execute(
        select(WorkoutSession)
        .where(
            WorkoutSession.user_id == user_id,
            WorkoutSession.session_date >= since,
            WorkoutSession.status == SessionStatus.finished,
        )
        .options(
            selectinload(WorkoutSession.sets),
            selectinload(WorkoutSession.template),
        )
        .order_by(WorkoutSession.session_date.asc(), WorkoutSession.id.asc())
    )
    sessions = list(result.scalars().all())

    bw = (
        await session.execute(
            select(BodyWeightLog)
            .where(BodyWeightLog.user_id == user_id)
            .order_by(BodyWeightLog.recorded_at.asc())
        )
    ).scalars().all()

    notes = (
        await session.execute(
            select(ExerciseNoteLog)
            .where(
                ExerciseNoteLog.user_id == user_id,
                ExerciseNoteLog.created_at
                >= datetime.combine(since, datetime.min.time()).replace(tzinfo=timezone.utc),
            )
            .order_by(ExerciseNoteLog.created_at.asc())
        )
    ).scalars().all()

    states = (
        await session.execute(
            select(UserExerciseState).where(UserExerciseState.user_id == user_id)
        )
    ).scalars().all()

    session_payloads: list[dict[str, Any]] = []
    for ws in sessions:
        ordered = sorted(ws.sets, key=lambda s: (s.created_at or datetime.min, s.id))
        set_rows: list[dict[str, Any]] = []
        prev_at = None
        for s in ordered:
            set_rows.append(
                {
                    "exercise_id": s.exercise_id,
                    "exercise_name": s.exercise_name,
                    "set_number": s.set_number,
                    "drop_index": s.drop_index,
                    "reps": s.reps,
                    "weight": s.weight,
                    "volume": s.volume,
                    "difficulty": s.difficulty.value if s.difficulty else None,
                    "rpe_1_10": s.rpe_1_10,
                    "created_at": _iso(s.created_at),
                    "rest_sec": _rest_seconds(prev_at, s.created_at),
                }
            )
            prev_at = s.created_at
        duration_sec = None
        if ws.started_at and ws.finished_at:
            duration_sec = _rest_seconds(ws.started_at, ws.finished_at)
        session_payloads.append(
            {
                "id": ws.id,
                "date": _iso(ws.session_date),
                "template_id": ws.template_id,
                "template_name": ws.template.name if ws.template else None,
                "status": ws.status.value,
                "started_at": _iso(ws.started_at),
                "finished_at": _iso(ws.finished_at),
                "duration_sec": duration_sec,
                "checkin": {
                    "energy_1_5": ws.energy_1_5,
                    "sleep_1_5": ws.sleep_1_5,
                    "pain_1_5": ws.pain_1_5,
                },
                "sets": set_rows,
            }
        )

    # Adherence vs schedule for the window
    finished_dates = {ws.session_date for ws in sessions}
    scheduled_days = 0
    logged_on_schedule = 0
    for offset in range(days):
        day = since + timedelta(days=offset)
        tpl = await get_template_for_weekday(session, day.weekday())
        if not tpl:
            continue
        scheduled_days += 1
        if day in finished_dates:
            logged_on_schedule += 1

    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "window_days": days,
        "user": {
            "id": user.id,
            "display_name": user.display_name,
            "short_code": user.short_code,
            "phase": user.phase.value if user.phase else None,
            "log_level": user.log_level.value if getattr(user, "log_level", None) else "minimal",
            "body_weight": user.body_weight,
            "height_cm": user.height_cm,
            "experience_months": effective_experience_months(user),
        },
        "body_weight_series": [
            {"weight": row.weight, "recorded_at": _iso(row.recorded_at)} for row in bw
        ],
        "notes_timeline": [
            {
                "exercise_id": n.exercise_id,
                "exercise_name": n.exercise_name,
                "session_id": n.session_id,
                "text": n.text,
                "cleared": bool(n.cleared),
                "created_at": _iso(n.created_at),
            }
            for n in notes
        ],
        "exercise_state": [
            {
                "exercise_id": st.exercise_id,
                "working_weight": st.working_weight,
                "suggested_weight": st.suggested_weight,
                "last_reps": st.last_reps,
                "last_sets": st.last_sets,
                "last_difficulty": st.last_difficulty.value if st.last_difficulty else None,
                "hard_streak": st.hard_streak,
                "current_note": st.note,
                "updated_at": _iso(st.updated_at),
            }
            for st in states
        ],
        "adherence": {
            "scheduled_days": scheduled_days,
            "logged_on_schedule": logged_on_schedule,
            "finished_sessions": len(sessions),
        },
        "sessions": session_payloads,
    }
