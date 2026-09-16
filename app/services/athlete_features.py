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
    SessionSet,
    SessionStatus,
    User,
    UserExerciseState,
    UserExerciseWeekPlan,
    WorkoutSession,
)
from app.services.reminders import get_template_for_weekday
from app.services.users import effective_experience_months

KG_FOLLOW_TOL = 0.5


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


def _followed_kg(actual: float | None, planned: float | None) -> bool | None:
    if actual is None or planned is None:
        return None
    try:
        return abs(float(actual) - float(planned)) <= KG_FOLLOW_TOL
    except (TypeError, ValueError):
        return None


def _followed_reps(actual: int | None, planned: int | None) -> bool | None:
    if actual is None or planned is None:
        return None
    try:
        return int(actual) == int(planned)
    except (TypeError, ValueError):
        return None


def _note_for_set(
    notes: list[Any],
    *,
    session_id: int | None,
    exercise_id: int | None,
    exercise_name: str | None,
) -> str | None:
    for n in reversed(notes):
        if session_id is not None and n.session_id == session_id:
            if exercise_id is not None and n.exercise_id == exercise_id:
                return (n.text or "").strip() or None
            if exercise_name and n.exercise_name == exercise_name:
                return (n.text or "").strip() or None
        if exercise_id is not None and n.exercise_id == exercise_id and not n.cleared:
            text = (n.text or "").strip()
            if text:
                return text
    return None


def _build_plan_adherence(
    session_payloads: list[dict[str, Any]],
    notes: list[Any],
) -> dict[str, Any]:
    """Aggregate planned vs actual from stamped SessionSet fields."""
    by_ex: dict[str, dict[str, Any]] = {}
    deviations: list[dict[str, Any]] = []

    for ws in session_payloads:
        sid = ws.get("id")
        for s in ws.get("sets") or []:
            pkg = s.get("planned_kg")
            preps = s.get("planned_reps")
            if pkg is None and preps is None:
                continue
            name = s.get("exercise_name") or "?"
            key = str(s.get("exercise_id") or name)
            slot = by_ex.setdefault(
                key,
                {
                    "exercise_id": s.get("exercise_id"),
                    "exercise_name": name,
                    "sets_n": 0,
                    "with_plan_n": 0,
                    "followed_n": 0,
                    "deviated_n": 0,
                    "kg_deltas": [],
                    "reps_deltas": [],
                },
            )
            slot["sets_n"] += 1
            slot["with_plan_n"] += 1
            fk = s.get("followed_kg")
            fr = s.get("followed_reps")
            followed = True
            if fk is False or fr is False:
                followed = False
            elif fk is None and fr is None:
                followed = False
            if followed:
                slot["followed_n"] += 1
            else:
                slot["deviated_n"] += 1
                note = _note_for_set(
                    notes,
                    session_id=sid if isinstance(sid, int) else None,
                    exercise_id=s.get("exercise_id"),
                    exercise_name=name,
                )
                dev: dict[str, Any] = {
                    "ex": name,
                    "n": s.get("set_number"),
                    "planned": {"kg": pkg, "reps": preps},
                    "actual": {"kg": s.get("weight"), "reps": s.get("reps")},
                    "psrc": s.get("plan_source"),
                    "date": ws.get("date"),
                }
                if note:
                    dev["note"] = note[:120]
                deviations.append(dev)
            try:
                if pkg is not None and s.get("weight") is not None:
                    slot["kg_deltas"].append(float(s["weight"]) - float(pkg))
            except (TypeError, ValueError):
                pass
            try:
                if preps is not None and s.get("reps") is not None:
                    slot["reps_deltas"].append(int(s["reps"]) - int(preps))
            except (TypeError, ValueError):
                pass

    exercises: list[dict[str, Any]] = []
    for slot in by_ex.values():
        kg_d = slot.pop("kg_deltas")
        reps_d = slot.pop("reps_deltas")
        slot["avg_kg_delta"] = (
            round(sum(kg_d) / len(kg_d), 2) if kg_d else None
        )
        slot["avg_reps_delta"] = (
            round(sum(reps_d) / len(reps_d), 2) if reps_d else None
        )
        exercises.append(slot)

    return {
        "exercises": exercises,
        "deviations": deviations[-40:],
        "with_plan_sets": sum(e["with_plan_n"] for e in exercises),
        "followed_sets": sum(e["followed_n"] for e in exercises),
        "deviated_sets": sum(e["deviated_n"] for e in exercises),
    }


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
            selectinload(WorkoutSession.sets).selectinload(SessionSet.exercise),
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
            select(UserExerciseState)
            .where(UserExerciseState.user_id == user_id)
            .options(selectinload(UserExerciseState.exercise))
        )
    ).scalars().all()

    from app.db.models import ExerciseArchive
    from app.services.archive import name_key
    from app.services.week_plan import monday_of, parse_sets_json, target_week_start

    arch_machines: dict[str, str] = {
        a.name_key: a.machine_name
        for a in (await session.execute(select(ExerciseArchive))).scalars().all()
        if a.machine_name
    }

    session_payloads: list[dict[str, Any]] = []
    for ws in sessions:
        ordered = sorted(ws.sets, key=lambda s: (s.created_at or datetime.min, s.id))
        set_rows: list[dict[str, Any]] = []
        for s in ordered:
            machine = s.exercise.machine_name if s.exercise else None
            if not machine and s.exercise_name:
                machine = arch_machines.get(name_key(s.exercise_name))
            planned_kg = getattr(s, "planned_kg", None)
            planned_reps = getattr(s, "planned_reps", None)
            planned_rpe = getattr(s, "planned_rpe", None)
            plan_source = getattr(s, "plan_source", None)
            row: dict[str, Any] = {
                "exercise_id": s.exercise_id,
                "exercise_name": s.exercise_name,
                "machine_name": machine,
                "set_number": s.set_number,
                "drop_index": s.drop_index,
                "reps": s.reps,
                "weight": s.weight,
                "volume": s.volume,
                "difficulty": s.difficulty.value if s.difficulty else None,
                "rpe_1_10": s.rpe_1_10,
                "created_at": _iso(s.created_at),
            }
            if planned_kg is not None or planned_reps is not None:
                row["planned_kg"] = planned_kg
                row["planned_reps"] = planned_reps
                row["planned_rpe"] = planned_rpe
                row["plan_source"] = plan_source
                row["followed_kg"] = _followed_kg(s.weight, planned_kg)
                row["followed_reps"] = _followed_reps(s.reps, planned_reps)
            set_rows.append(row)
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

    plan_adherence = _build_plan_adherence(session_payloads, list(notes))

    today = date.today()
    week_starts = {
        monday_of(today) - timedelta(days=7),
        monday_of(today),
        target_week_start(today),
    }
    week_plan_rows = (
        await session.execute(
            select(UserExerciseWeekPlan)
            .where(
                UserExerciseWeekPlan.user_id == user_id,
                UserExerciseWeekPlan.week_start.in_(sorted(week_starts)),
            )
            .options(selectinload(UserExerciseWeekPlan.exercise))
        )
    ).scalars().all()
    week_plans: list[dict[str, Any]] = []
    for plan in week_plan_rows:
        advice = (plan.advice or "").strip()
        week_plans.append(
            {
                "exercise_id": plan.exercise_id,
                "exercise_name": plan.exercise.name if plan.exercise else None,
                "week_start": plan.week_start.isoformat(),
                "sets": parse_sets_json(plan.sets_json),
                "advice": advice[:200] if advice else None,
            }
        )

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
            "sex": getattr(user, "sex", None),
            "age": getattr(user, "age", None),
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
                "exercise_name": st.exercise.name if st.exercise else None,
                "machine_name": (
                    (st.exercise.machine_name if st.exercise else None)
                    or (
                        arch_machines.get(name_key(st.exercise.name))
                        if st.exercise and st.exercise.name
                        else None
                    )
                ),
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
        "plan_adherence": plan_adherence,
        "week_plans": week_plans,
        "sessions": session_payloads,
    }
