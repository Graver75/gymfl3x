from __future__ import annotations

from collections import defaultdict
from datetime import date

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.db.models import SessionStatus, WorkoutSession, WorkoutTemplate
from app.services.progression import DIFFICULTY_LABELS, format_session_exercise


async def build_group_recap(session: AsyncSession, template: WorkoutTemplate, day: date) -> str:
    """Build chat recap.

    #деньспины
    Упражнение
    И 12×55, 10×50+8×40 легко
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
        grouped: dict[str, list] = defaultdict(list)
        for sset in ws.sets:
            grouped[sset.exercise_name].append(sset)
        for name, rows in grouped.items():
            if name not in by_exercise:
                order.append(name)
            diff = DIFFICULTY_LABELS[rows[-1].difficulty]
            by_exercise[name].append(f"{code} {format_session_exercise(rows)} {diff}")

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

    grouped_cur: dict[str, list] = defaultdict(list)
    for s in current.sets:
        grouped_cur[s.exercise_name].append(s)

    cur_volume = sum(s.volume for s in current.sets)
    lines = [
        f"Ретроспектива: {title}",
        f"Упражнений: {len(grouped_cur)}",
        f"Объём: {cur_volume:g} кг·повт",
    ]

    if previous:
        grouped_prev: dict[str, list] = defaultdict(list)
        for s in previous.sets:
            grouped_prev[s.exercise_name].append(s)
        prev_volume = sum(s.volume for s in previous.sets)
        delta = cur_volume - prev_volume
        if prev_volume:
            pct = (delta / prev_volume) * 100
            sign = "+" if delta >= 0 else ""
            lines.append(f"Vs прошлый раз: {sign}{delta:g} ({sign}{pct:.0f}%)")
        else:
            lines.append("Есть с чем сравнивать в следующий раз.")

        progressed: list[str] = []
        stuck: list[str] = []
        for name, rows in grouped_cur.items():
            old_rows = grouped_prev.get(name)
            cur_w = max(s.weight for s in rows)
            if old_rows:
                old_w = max(s.weight for s in old_rows)
                if cur_w > old_w or sum(s.volume for s in rows) > sum(s.volume for s in old_rows):
                    progressed.append(
                        f"• {name}: {format_session_exercise(old_rows)} -> {format_session_exercise(rows)}"
                    )
            if rows[-1].difficulty.value in ("hard", "failure"):
                stuck.append(f"• {name} — тяжело/отказ")
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
