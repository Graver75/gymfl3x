from __future__ import annotations

from collections import defaultdict
from datetime import date

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.db.models import SessionStatus, WorkoutSession, WorkoutTemplate
from app.services.progression import DIFFICULTY_LABELS, format_block


async def build_group_recap(session: AsyncSession, template: WorkoutTemplate, day: date) -> str:
    """Build chat recap in the familiar format:

    #деньспины
    Упражнение
    И 12×55×4 легко
    К 12×50×4 норм
    """
    result = await session.execute(
        select(WorkoutSession)
        .where(
            WorkoutSession.session_date == day,
            WorkoutSession.template_id == template.id,
            WorkoutSession.status == SessionStatus.finished,
        )
        .options(
            selectinload(WorkoutSession.user),
            selectinload(WorkoutSession.sets),
        )
    )
    sessions = list(result.scalars().all())
    if not sessions:
        return f"#{template.hashtag}\nПока никто не залогировал тренировку."

    by_exercise: dict[str, list[str]] = defaultdict(list)
    order: list[str] = []

    for ws in sorted(sessions, key=lambda s: s.user.short_code):
        code = ws.user.short_code
        for sset in ws.sets:
            name = sset.exercise_name
            if name not in by_exercise:
                order.append(name)
            line = (
                f"{code} {format_block(sset.reps, sset.weight, sset.sets_count)} "
                f"{DIFFICULTY_LABELS[sset.difficulty]}"
            )
            by_exercise[name].append(line)

    lines = [f"#{template.hashtag}"]
    for name in order:
        lines.append(name)
        lines.extend(by_exercise[name])
        lines.append("")
    return "\n".join(lines).rstrip()


async def build_personal_retrospective(
    session: AsyncSession,
    current: WorkoutSession,
) -> str:
    """Compare current finished session with previous same-template session."""
    result = await session.execute(
        select(WorkoutSession)
        .where(WorkoutSession.id == current.id)
        .options(
            selectinload(WorkoutSession.sets),
            selectinload(WorkoutSession.user),
            selectinload(WorkoutSession.template),
        )
    )
    current = result.scalar_one()
    template = current.template
    title = template.name if template else "Тренировка"

    prev_result = await session.execute(
        select(WorkoutSession)
        .where(
            WorkoutSession.user_id == current.user_id,
            WorkoutSession.template_id == current.template_id,
            WorkoutSession.status == SessionStatus.finished,
            WorkoutSession.id != current.id,
        )
        .options(selectinload(WorkoutSession.sets))
        .order_by(WorkoutSession.session_date.desc(), WorkoutSession.id.desc())
        .limit(1)
    )
    previous = prev_result.scalar_one_or_none()

    cur_volume = sum(s.volume for s in current.sets)
    lines = [
        f"Ретроспектива: {title}",
        f"Упражнений: {len(current.sets)}",
        f"Объём: {cur_volume:g} кг·повт",
    ]

    if previous:
        prev_volume = sum(s.volume for s in previous.sets)
        delta = cur_volume - prev_volume
        if prev_volume:
            pct = (delta / prev_volume) * 100
            sign = "+" if delta >= 0 else ""
            lines.append(f"Vs прошлый раз: {sign}{delta:g} ({sign}{pct:.0f}%)")
        else:
            lines.append("Есть с чем сравнивать в следующий раз.")

        prev_by_name = {s.exercise_name: s for s in previous.sets}
        progressed: list[str] = []
        stuck: list[str] = []
        for sset in current.sets:
            old = prev_by_name.get(sset.exercise_name)
            if not old:
                continue
            if sset.weight > old.weight or (
                sset.weight == old.weight and sset.reps > old.reps
            ):
                progressed.append(
                    f"• {sset.exercise_name}: "
                    f"{format_block(old.reps, old.weight, old.sets_count)} -> "
                    f"{format_block(sset.reps, sset.weight, sset.sets_count)}"
                )
            if sset.difficulty.value in ("hard", "failure"):
                stuck.append(f"• {sset.exercise_name} — тяжело/отказ")
        if progressed:
            lines.append("")
            lines.append("Прогресс:")
            lines.extend(progressed)
        if stuck:
            lines.append("")
            lines.append("На заметку:")
            lines.extend(stuck)
    else:
        lines.append("Первая тренировка этого дня — база зафиксирована.")

    lines.append("")
    lines.append("На следующий раз бот подставит предложенные веса.")
    return "\n".join(lines)
