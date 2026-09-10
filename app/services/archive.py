from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import ExerciseArchive, SessionSet, TemplateExercise, WorkoutTemplate


def name_key(name: str) -> str:
    return " ".join(name.strip().split()).lower()


async def upsert_archive(
    session: AsyncSession,
    name: str,
    *,
    target_sets: int | None = None,
    target_reps_min: int | None = None,
    target_reps_max: int | None = None,
    weight_step: float | None = None,
    overwrite_targets: bool = False,
) -> ExerciseArchive | None:
    cleaned = " ".join(name.strip().split())
    if not cleaned:
        return None
    key = name_key(cleaned)
    result = await session.execute(select(ExerciseArchive).where(ExerciseArchive.name_key == key))
    item = result.scalar_one_or_none()
    if item is None:
        item = ExerciseArchive(
            name=cleaned[:128],
            name_key=key,
            target_sets=target_sets or 3,
            target_reps_min=target_reps_min or 8,
            target_reps_max=target_reps_max or 12,
            weight_step=weight_step if weight_step is not None else 2.5,
        )
        session.add(item)
        return item
    item.name = cleaned[:128]
    if overwrite_targets:
        if target_sets is not None:
            item.target_sets = target_sets
        if target_reps_min is not None:
            item.target_reps_min = target_reps_min
        if target_reps_max is not None:
            item.target_reps_max = target_reps_max
        if weight_step is not None:
            item.weight_step = weight_step
    return item


async def list_archive(session: AsyncSession) -> list[ExerciseArchive]:
    result = await session.execute(select(ExerciseArchive).order_by(ExerciseArchive.name))
    return list(result.scalars().all())


async def sync_catalog(session: AsyncSession) -> list[ExerciseArchive]:
    """Archive + all template (active) names — any exercise ever added to a program."""
    await backfill_archive(session)
    return await list_archive(session)


async def backfill_archive(session: AsyncSession) -> int:
    before = (await session.execute(select(ExerciseArchive))).scalars().all()
    start = len(before)

    templates = (await session.execute(select(TemplateExercise))).scalars().all()
    for ex in templates:
        await upsert_archive(
            session,
            ex.name,
            target_sets=ex.target_sets,
            target_reps_min=ex.target_reps_min,
            target_reps_max=ex.target_reps_max,
            weight_step=ex.weight_step,
            overwrite_targets=True,
        )

    names = (await session.execute(select(SessionSet.exercise_name).distinct())).scalars().all()
    for raw in names:
        await upsert_archive(session, raw, overwrite_targets=False)

    await session.commit()
    after = (await session.execute(select(ExerciseArchive))).scalars().all()
    return len(after) - start


async def add_exercise_to_template(
    session: AsyncSession,
    template_id: int,
    *,
    name: str,
    target_sets: int,
    target_reps_min: int,
    target_reps_max: int,
    weight_step: float,
) -> tuple[TemplateExercise | None, str]:
    tpl = await session.get(WorkoutTemplate, template_id)
    if not tpl:
        return None, "Шаблон не найден"

    key = name_key(name)
    existing = (
        await session.execute(
            select(TemplateExercise).where(TemplateExercise.template_id == template_id)
        )
    ).scalars().all()
    if any(name_key(ex.name) == key for ex in existing):
        return None, "Это упражнение уже есть в шаблоне"

    last = max(existing, key=lambda e: e.position) if existing else None
    pos = (last.position + 1) if last else 0
    item = TemplateExercise(
        template_id=template_id,
        name=name.strip()[:128],
        position=pos,
        target_sets=target_sets,
        target_reps_min=target_reps_min,
        target_reps_max=target_reps_max,
        weight_step=weight_step,
    )
    session.add(item)
    await upsert_archive(
        session,
        name,
        target_sets=target_sets,
        target_reps_min=target_reps_min,
        target_reps_max=target_reps_max,
        weight_step=weight_step,
        overwrite_targets=True,
    )
    return item, "ok"
