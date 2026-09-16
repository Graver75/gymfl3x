from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import ExerciseArchive, SessionSet, TemplateExercise, WorkoutTemplate


def name_key(name: str) -> str:
    return " ".join(name.strip().split()).lower()


def _norm_machine(machine_name: str | None) -> str | None:
    if machine_name is None:
        return None
    cleaned = " ".join(str(machine_name).strip().split())
    return cleaned[:128] or None


async def upsert_archive(
    session: AsyncSession,
    name: str,
    *,
    target_sets: int | None = None,
    target_reps_min: int | None = None,
    target_reps_max: int | None = None,
    weight_step: float | None = None,
    machine_name: str | None = None,
    overwrite_targets: bool = False,
    overwrite_machine: bool = False,
) -> ExerciseArchive | None:
    cleaned = " ".join(name.strip().split())
    if not cleaned:
        return None
    key = name_key(cleaned)
    result = await session.execute(select(ExerciseArchive).where(ExerciseArchive.name_key == key))
    item = result.scalar_one_or_none()
    machine = _norm_machine(machine_name)
    if item is None:
        item = ExerciseArchive(
            name=cleaned[:128],
            name_key=key,
            machine_name=machine,
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
    if overwrite_machine:
        item.machine_name = machine
    elif machine and not item.machine_name:
        # Fill empty archive machine from a known value without wiping others
        item.machine_name = machine
    return item


async def list_instances_by_name(
    session: AsyncSession, name: str
) -> list[TemplateExercise]:
    key = name_key(name)
    rows = (await session.execute(select(TemplateExercise))).scalars().all()
    return [ex for ex in rows if name_key(ex.name) == key]


async def set_machine_for_exercise_name(
    session: AsyncSession,
    name: str,
    machine_name: str | None,
) -> int:
    """Set machine on archive + all TemplateExercise copies. Returns instance count."""
    machine = _norm_machine(machine_name)
    await upsert_archive(session, name, machine_name=machine, overwrite_machine=True)
    instances = await list_instances_by_name(session, name)
    for ex in instances:
        ex.machine_name = machine
    return len(instances)

async def resolve_machine_for_name(
    session: AsyncSession, name: str
) -> str | None:
    key = name_key(name)
    arch = (
        await session.execute(select(ExerciseArchive).where(ExerciseArchive.name_key == key))
    ).scalar_one_or_none()
    if arch and arch.machine_name:
        return arch.machine_name
    for ex in await list_instances_by_name(session, name):
        if ex.machine_name:
            return ex.machine_name
    return None


async def sync_machines_from_archive(session: AsyncSession) -> int:
    """
    One-shot / startup reconcile:
    1) Prefer non-empty TemplateExercise.machine → archive (if archive empty)
    2) Prefer archive.machine → all TemplateExercise copies
    3) If multiple TE disagree, pick the first non-empty and propagate
    Returns number of TemplateExercise rows touched.
    """
    archives = {
        a.name_key: a
        for a in (await session.execute(select(ExerciseArchive))).scalars().all()
    }
    by_key: dict[str, list[TemplateExercise]] = {}
    for ex in (await session.execute(select(TemplateExercise))).scalars().all():
        by_key.setdefault(name_key(ex.name), []).append(ex)

    touched = 0
    all_keys = set(archives) | set(by_key)
    for key in all_keys:
        instances = by_key.get(key) or []
        arch = archives.get(key)

        # Collect candidates
        candidates: list[str] = []
        if arch and arch.machine_name:
            candidates.append(arch.machine_name)
        for ex in instances:
            if ex.machine_name:
                candidates.append(ex.machine_name)

        # Canonical: archive wins if set; else first non-empty from instances
        if arch and arch.machine_name:
            canonical = _norm_machine(arch.machine_name)
        elif candidates:
            canonical = _norm_machine(candidates[0])
        else:
            canonical = None

        if arch is None and instances:
            arch = await upsert_archive(
                session,
                instances[0].name,
                target_sets=instances[0].target_sets,
                target_reps_min=instances[0].target_reps_min,
                target_reps_max=instances[0].target_reps_max,
                weight_step=instances[0].weight_step,
                machine_name=canonical,
                overwrite_machine=True,
                overwrite_targets=True,
            )
            if arch:
                archives[key] = arch
        elif arch is not None and arch.machine_name != canonical:
            arch.machine_name = canonical

        for ex in instances:
            if ex.machine_name != canonical:
                ex.machine_name = canonical
                touched += 1

    await session.commit()
    return touched


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
            machine_name=ex.machine_name,
            overwrite_targets=True,
            overwrite_machine=False,
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
    machine_name: str | None = None,
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

    # Prefer explicit machine, else archive prototype
    machine = _norm_machine(machine_name)
    if machine is None:
        machine = await resolve_machine_for_name(session, name)

    last = max(existing, key=lambda e: e.position) if existing else None
    pos = (last.position + 1) if last else 0
    item = TemplateExercise(
        template_id=template_id,
        name=name.strip()[:128],
        machine_name=machine,
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
        machine_name=machine,
        overwrite_targets=True,
        overwrite_machine=bool(machine_name is not None),
    )
    # Keep siblings in other templates in sync if we set/learned a machine
    if machine is not None:
        await set_machine_for_exercise_name(session, name, machine)
    return item, "ok"
