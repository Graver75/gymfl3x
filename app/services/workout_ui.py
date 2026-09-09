from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import selectinload

from app.db.models import TemplateExercise, UserExerciseState, WorkoutSession, WorkoutTemplate
from app import ui_copy as ui


@dataclass
class FreeExercise:
    id: int | None
    name: str
    target_sets: int = 3
    target_reps_min: int = 8
    target_reps_max: int = 12
    weight_step: float = 2.5


def touch_action_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def rest_line(data: dict) -> str:
    raw = data.get("last_action_at")
    if not raw:
        return ""
    try:
        started = datetime.fromisoformat(str(raw))
        if started.tzinfo is None:
            started = started.replace(tzinfo=timezone.utc)
        elapsed = int((datetime.now(timezone.utc) - started).total_seconds())
    except ValueError:
        return ""
    if elapsed < 0:
        elapsed = 0
    minutes, seconds = divmod(elapsed, 60)
    if minutes >= 60:
        hours, minutes = divmod(minutes, 60)
        return f"\n{ui.ICO_REST} Отдых: {hours}:{minutes:02d}:{seconds:02d}"
    return f"\n{ui.ICO_REST} Отдых: {minutes}:{seconds:02d}"


async def weight_hints_for_user(session, user_id: int, exercises: list) -> dict[int, float]:
    hints: dict[int, float] = {}
    ids = [ex.id for ex in exercises if getattr(ex, "id", None) is not None]
    if not ids:
        return hints
    result = await session.execute(
        select(UserExerciseState).where(
            UserExerciseState.user_id == user_id,
            UserExerciseState.exercise_id.in_(ids),
        )
    )
    for st in result.scalars().all():
        w = st.suggested_weight if st.suggested_weight is not None else st.working_weight
        if w is not None:
            hints[st.exercise_id] = float(w)
    return hints


def next_exercise_id(exercises: list, done_ids: set[int]) -> int | None:
    for ex in exercises:
        if ex.id not in done_ids:
            return ex.id
    return None


def session_map_text(template_name: str, exercises: list, done_ids: set[int]) -> str:
    total = len(exercises)
    done = sum(1 for ex in exercises if ex.id in done_ids)
    lines = [f"{ui.ICO_MAP} {template_name}", f"Прогресс: {done}/{total}", ""]
    for ex in exercises:
        if ex.id in done_ids:
            mark = ui.ICO_DONE
        elif next_exercise_id(exercises, done_ids) == ex.id:
            mark = ui.ICO_NEXT
        else:
            mark = "▫️"
        lines.append(
            f"{mark} {ex.name} ({ex.target_sets}×{ex.target_reps_min}-{ex.target_reps_max})"
        )
    return "\n".join(lines)


async def load_template_with_exercises(session, template_id: int) -> WorkoutTemplate | None:
    result = await session.execute(
        select(WorkoutTemplate)
        .where(WorkoutTemplate.id == template_id)
        .options(selectinload(WorkoutTemplate.exercises))
    )
    return result.scalar_one_or_none()


def free_from_archive(item) -> FreeExercise:
    return FreeExercise(
        id=None,
        name=item.name,
        target_sets=item.target_sets or 3,
        target_reps_min=item.target_reps_min or 8,
        target_reps_max=item.target_reps_max or 12,
        weight_step=item.weight_step or 2.5,
    )


async def resolve_exercise(session, data: dict):
    exercise_id = data.get("exercise_id")
    if exercise_id:
        return await session.get(TemplateExercise, exercise_id)
    free = data.get("free_exercise") or {}
    if free.get("name"):
        return FreeExercise(
            id=None,
            name=free["name"],
            target_sets=int(free.get("target_sets") or 3),
            target_reps_min=int(free.get("target_reps_min") or 8),
            target_reps_max=int(free.get("target_reps_max") or 12),
            weight_step=float(free.get("weight_step") or 2.5),
        )
    return None
