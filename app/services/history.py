from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.db.models import ExerciseNoteLog, SessionSet, SessionStatus, WorkoutSession
from app.services.archive import name_key
from app.services.progression import DIFFICULTY_LABELS, format_session_exercise
from app import ui_copy as ui


@dataclass
class SmartPresets:
    default_weight: float | None = None
    weight_options: list[float] = field(default_factory=list)
    default_reps: int | None = None
    reps_options: list[int] = field(default_factory=list)
    last_pattern_text: str | None = None
    last_parts: list[dict] = field(default_factory=list)
    last_drop_weights: list[float] = field(default_factory=list)
    last_date: str | None = None
    last_difficulty: str | None = None


async def list_recent_sessions(
    session: AsyncSession,
    user_id: int,
    *,
    limit: int = 10,
) -> list[WorkoutSession]:
    result = await session.execute(
        select(WorkoutSession)
        .where(
            WorkoutSession.user_id == user_id,
            WorkoutSession.status == SessionStatus.finished,
        )
        .options(
            selectinload(WorkoutSession.template),
            selectinload(WorkoutSession.sets),
        )
        .order_by(WorkoutSession.session_date.desc(), WorkoutSession.id.desc())
        .limit(limit)
    )
    return list(result.scalars().all())


async def list_athletes(session: AsyncSession) -> list:
    from app.db.models import User

    result = await session.execute(
        select(User)
        .where(User.onboarding_done.is_(True))
        .order_by(User.short_code, User.display_name)
    )
    return list(result.scalars().all())


async def get_athlete(session: AsyncSession, user_id: int):
    from app.db.models import User

    return await session.get(User, user_id)


def athlete_label(user) -> str:
    return f"{user.short_code} · {user.display_name}"


async def get_user_session(
    session: AsyncSession,
    user_id: int,
    session_id: int,
) -> WorkoutSession | None:
    result = await session.execute(
        select(WorkoutSession)
        .where(
            WorkoutSession.id == session_id,
            WorkoutSession.user_id == user_id,
        )
        .options(
            selectinload(WorkoutSession.template),
            selectinload(WorkoutSession.sets),
        )
    )
    return result.scalar_one_or_none()


async def list_user_exercise_names(session: AsyncSession, user_id: int) -> list[str]:
    result = await session.execute(
        select(SessionSet.exercise_name)
        .join(WorkoutSession, SessionSet.session_id == WorkoutSession.id)
        .where(
            WorkoutSession.user_id == user_id,
            WorkoutSession.status == SessionStatus.finished,
        )
        .distinct()
    )
    names = sorted({n for n in result.scalars().all() if n}, key=lambda s: s.lower())
    return names


async def _sessions_with_exercise(
    session: AsyncSession,
    user_id: int,
    *,
    exercise_id: int | None,
    exercise_name: str | None,
    limit: int = 5,
) -> list[WorkoutSession]:
    key = name_key(exercise_name) if exercise_name else None
    result = await session.execute(
        select(WorkoutSession)
        .where(
            WorkoutSession.user_id == user_id,
            WorkoutSession.status == SessionStatus.finished,
        )
        .options(selectinload(WorkoutSession.sets))
        .order_by(WorkoutSession.session_date.desc(), WorkoutSession.id.desc())
        .limit(40)
    )
    matched: list[WorkoutSession] = []
    for ws in result.scalars().all():
        rows = []
        for s in ws.sets:
            if exercise_id and s.exercise_id == exercise_id:
                rows.append(s)
            elif key and name_key(s.exercise_name) == key:
                rows.append(s)
        if rows:
            matched.append(ws)
            if len(matched) >= limit:
                break
    return matched


def _parts_from_sets(sets: list[SessionSet]) -> list[dict]:
    numbered = [s for s in sets if (s.set_number or 0) > 0]
    source = numbered or sets
    parts = []
    for i, s in enumerate(source):
        parts.append(
            {
                "set_number": int(s.set_number or (i + 1)),
                "drop_index": int(s.drop_index or 0),
                "weight": float(s.weight),
                "reps": int(s.reps),
            }
        )
    return parts


async def build_smart_presets(
    session: AsyncSession,
    user_id: int,
    *,
    exercise_id: int | None,
    exercise_name: str,
    suggested_weight: float | None = None,
    working_weight: float | None = None,
    set_number: int = 1,
    drop_index: int = 0,
) -> SmartPresets:
    presets = SmartPresets()
    history = await _sessions_with_exercise(
        session,
        user_id,
        exercise_id=exercise_id,
        exercise_name=exercise_name,
        limit=5,
    )
    if not history:
        presets.default_weight = suggested_weight or working_weight or 20.0
        presets.weight_options = _unique_weights(
            [presets.default_weight, (presets.default_weight or 20) - 2.5, (presets.default_weight or 20) + 2.5]
        )
        presets.reps_options = [8, 10, 12, 15]
        return presets

    last = history[0]
    last_sets = [
        s
        for s in last.sets
        if (exercise_id and s.exercise_id == exercise_id)
        or name_key(s.exercise_name) == name_key(exercise_name)
    ]
    presets.last_parts = _parts_from_sets(last_sets)
    presets.last_pattern_text = format_session_exercise(last_sets)
    presets.last_date = ui.format_user_date(last.session_date)
    if last_sets:
        presets.last_difficulty = DIFFICULTY_LABELS.get(last_sets[-1].difficulty, None)

    weight_counter: Counter[float] = Counter()
    reps_counter: Counter[int] = Counter()
    drop_weights: list[float] = []
    for ws in history:
        rows = [
            s
            for s in ws.sets
            if (exercise_id and s.exercise_id == exercise_id)
            or name_key(s.exercise_name) == name_key(exercise_name)
        ]
        for s in rows:
            weight_counter[float(s.weight)] += 1
            if s.reps > 0:
                reps_counter[int(s.reps)] += 1
            if (s.drop_index or 0) > 0:
                drop_weights.append(float(s.weight))

    same_set_mains = [
        p for p in presets.last_parts if int(p["set_number"]) == set_number and int(p["drop_index"]) == 0
    ]
    same_set_drops = [
        p for p in presets.last_parts if int(p["set_number"]) == set_number and int(p["drop_index"]) > 0
    ]

    # Prefer last session pattern over stale suggested/working when we have history.
    if drop_index > 0 and same_set_drops:
        idx = min(drop_index - 1, len(same_set_drops) - 1)
        presets.default_weight = float(same_set_drops[idx]["weight"])
        presets.default_reps = int(same_set_drops[idx]["reps"]) or None
    elif drop_index > 0:
        presets.default_weight = None
    elif same_set_mains:
        presets.default_weight = float(same_set_mains[0]["weight"])
        presets.default_reps = int(same_set_mains[0]["reps"]) or None
    elif presets.last_parts:
        mains = [p for p in presets.last_parts if int(p["drop_index"]) == 0]
        pick = mains[-1] if mains else presets.last_parts[-1]
        presets.default_weight = float(pick["weight"])
        presets.default_reps = int(pick["reps"]) or None
    elif suggested_weight is not None:
        presets.default_weight = suggested_weight
    elif working_weight is not None:
        presets.default_weight = working_weight

    if presets.default_reps is None and same_set_mains:
        presets.default_reps = int(same_set_mains[0]["reps"]) or None
    if presets.default_reps is None and reps_counter:
        presets.default_reps = reps_counter.most_common(1)[0][0]

    base = presets.default_weight
    if base is None:
        base = suggested_weight if suggested_weight is not None else working_weight
    if base is None:
        base = 20.0
    presets.default_weight = float(base)

    # Nearby plate steps + history only if close to base (no wild jumps).
    step = 2.5
    max_delta = max(step * 4, float(base) * 0.75, 5.0)
    nearby = [
        max(0.0, float(base) - step * 2),
        max(0.0, float(base) - step),
        float(base),
        float(base) + step,
        float(base) + step * 2,
    ]
    candidates: list[float | None] = list(nearby)
    candidates.extend(float(p["weight"]) for p in presets.last_parts)
    candidates.extend(w for w, _ in weight_counter.most_common(8))
    if suggested_weight is not None:
        candidates.append(suggested_weight)
    if working_weight is not None:
        candidates.append(working_weight)

    filtered = [
        w
        for w in _unique_weights(candidates)
        if abs(float(w) - float(base)) <= max_delta + 1e-6
    ]
    if float(base) not in filtered:
        filtered.insert(0, float(base))
    presets.weight_options = filtered[:6]

    top_reps = [r for r, _ in reps_counter.most_common(6)]
    reps_candidates = [8, 10, 12, 15]
    if presets.default_reps:
        reps_candidates.insert(0, presets.default_reps)
    reps_candidates.extend(top_reps)
    presets.reps_options = _unique_ints(reps_candidates)[:6]

    presets.last_drop_weights = _unique_weights(
        [
            w
            for w in drop_weights + [float(p["weight"]) for p in same_set_drops]
            if abs(float(w) - float(base)) <= max_delta + 1e-6
        ]
    )[:4]
    return presets


def _unique_weights(values: list[float | None]) -> list[float]:
    seen: set[float] = set()
    out: list[float] = []
    for v in values:
        if v is None:
            continue
        key = round(float(v), 2)
        if key in seen or key < 0:
            continue
        seen.add(key)
        out.append(float(v))
    return out


def _unique_ints(values: list[int | None]) -> list[int]:
    seen: set[int] = set()
    out: list[int] = []
    for v in values:
        if v is None or v <= 0:
            continue
        if v in seen:
            continue
        seen.add(v)
        out.append(int(v))
    return out


def _format_set_line(s: SessionSet) -> str:
    if (s.set_number or 0) > 0:
        label = f"#{s.set_number}"
        if s.drop_index:
            label += f".{s.drop_index}"
        base = f"{label} {s.reps}×{s.weight:g}"
    else:
        base = f"{s.reps}×{s.weight:g}"
    if s.rpe_1_10 is not None:
        base += f" · RPE{s.rpe_1_10}"
    return base


def _session_duration_label(ws: WorkoutSession) -> str | None:
    if not ws.started_at or not ws.finished_at:
        return None
    started = ws.started_at
    finished = ws.finished_at
    if started.tzinfo is None or finished.tzinfo is None:
        delta = finished.replace(tzinfo=None) - started.replace(tzinfo=None)
    else:
        delta = finished - started
    total = int(delta.total_seconds())
    if total < 0:
        return None
    minutes, seconds = divmod(total, 60)
    hours, minutes = divmod(minutes, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{seconds:02d}"
    return f"{minutes}:{seconds:02d}"


def format_session_history(
    ws: WorkoutSession,
    *,
    notes: list | None = None,
) -> str:
    title = ws.template.name if ws.template else "Тренировка"
    lines = [f"{ui.ICO_HISTORY} {title} · {ui.format_user_date(ws.session_date)}"]

    meta: list[str] = []
    duration = _session_duration_label(ws)
    if duration:
        meta.append(f"{ui.ICO_REST} {duration}")
    total_vol = sum(float(s.volume or 0) for s in ws.sets)
    if total_vol:
        meta.append(f"объём {total_vol:g}")
    if meta:
        lines.append(" · ".join(meta))

    checkin_bits = []
    if ws.energy_1_5 is not None:
        checkin_bits.append(f"энергия {ws.energy_1_5}")
    if ws.sleep_1_5 is not None:
        checkin_bits.append(f"сон {ws.sleep_1_5}")
    if ws.pain_1_5 is not None:
        checkin_bits.append(f"боль {ws.pain_1_5}")
    if checkin_bits:
        lines.append(f"Чекин: {' · '.join(checkin_bits)}")

    lines.append("")

    grouped: dict[str, list] = defaultdict(list)
    for s in sorted(ws.sets, key=lambda x: (x.id,)):
        grouped[s.exercise_name].append(s)
    if not grouped:
        lines.append("Пусто")
        return "\n".join(lines)

    notes_by_ex: dict[str, list] = defaultdict(list)
    for n in notes or []:
        notes_by_ex[name_key(n.exercise_name)].append(n)

    for name, rows in grouped.items():
        rows_sorted = sorted(
            rows,
            key=lambda s: (s.set_number or 0, s.drop_index or 0, s.id),
        )
        diff = DIFFICULTY_LABELS.get(rows_sorted[-1].difficulty, "")
        lines.append(f"{ui.ICO_EXERCISE} {name}")
        for s in rows_sorted:
            lines.append(f"  {_format_set_line(s)}")
        summary = format_session_exercise(rows_sorted)
        footer = f"  ∑ {summary}"
        if diff:
            footer += f" · {diff}"
        lines.append(footer)
        for n in notes_by_ex.get(name_key(name), []):
            if n.cleared:
                lines.append(f"  {ui.ICO_NOTE} (очищена)")
            elif n.text:
                lines.append(f"  {ui.ICO_NOTE} {n.text}")
        lines.append("")

    return "\n".join(lines).rstrip()


async def session_notes(
    session: AsyncSession,
    session_id: int,
) -> list[ExerciseNoteLog]:
    result = await session.execute(
        select(ExerciseNoteLog)
        .where(ExerciseNoteLog.session_id == session_id)
        .order_by(ExerciseNoteLog.created_at.asc())
    )
    return list(result.scalars().all())


async def delete_workout_session(
    session: AsyncSession,
    *,
    owner_user_id: int,
    session_id: int,
) -> bool:
    """Delete a finished/active session and NN-related note logs for it."""
    ws = await get_user_session(session, owner_user_id, session_id)
    if not ws:
        return False
    notes = await session.execute(
        select(ExerciseNoteLog).where(ExerciseNoteLog.session_id == session_id)
    )
    for note in notes.scalars().all():
        await session.delete(note)
    await session.delete(ws)
    await session.commit()
    return True


async def format_exercise_history(
    session: AsyncSession,
    user_id: int,
    exercise_name: str,
    *,
    limit: int = 5,
) -> str:
    history = await _sessions_with_exercise(
        session,
        user_id,
        exercise_id=None,
        exercise_name=exercise_name,
        limit=limit,
    )
    if not history:
        return f"{ui.label_exercise(exercise_name)}\nПока нет записей."
    pr = await exercise_personal_records(session, user_id, exercise_name=exercise_name)
    lines = [ui.label_exercise(exercise_name)]
    if pr["best_weight"] is not None:
        lines.append(
            f"{ui.ICO_PR} PR: {pr['best_weight']:g} кг · лучший сет {pr['best_reps']}×{pr['best_set_weight']:g}"
        )
    lines.append("")
    for ws in history:
        rows = [s for s in ws.sets if name_key(s.exercise_name) == name_key(exercise_name)]
        rows = sorted(rows, key=lambda s: (s.set_number or 0, s.drop_index or 0, s.id))
        diff = DIFFICULTY_LABELS.get(rows[-1].difficulty, "") if rows else ""
        set_bits = []
        for s in rows:
            bit = f"{s.reps}×{s.weight:g}"
            if s.rpe_1_10 is not None:
                bit += f"@{s.rpe_1_10}"
            set_bits.append(bit)
        line = f"{ui.format_user_date(ws.session_date)}: {', '.join(set_bits) or format_session_exercise(rows)}"
        if diff:
            line += f" · {diff}"
        # session checkin snippet if any
        chk = []
        if ws.energy_1_5 is not None:
            chk.append(f"E{ws.energy_1_5}")
        if ws.sleep_1_5 is not None:
            chk.append(f"S{ws.sleep_1_5}")
        if ws.pain_1_5 is not None:
            chk.append(f"P{ws.pain_1_5}")
        if chk:
            line += f" · {'/'.join(chk)}"
        lines.append(line)
    return "\n".join(lines)


async def exercise_personal_records(
    session: AsyncSession,
    user_id: int,
    *,
    exercise_id: int | None = None,
    exercise_name: str | None = None,
) -> dict:
    history = await _sessions_with_exercise(
        session,
        user_id,
        exercise_id=exercise_id,
        exercise_name=exercise_name,
        limit=40,
    )
    best_weight = None
    best_reps = None
    best_set_weight = None
    best_score = -1.0
    for ws in history:
        for s in ws.sets:
            if exercise_id and s.exercise_id != exercise_id:
                if not (exercise_name and name_key(s.exercise_name) == name_key(exercise_name)):
                    continue
            elif exercise_name and name_key(s.exercise_name) != name_key(exercise_name):
                continue
            w = float(s.weight)
            r = int(s.reps)
            if best_weight is None or w > best_weight:
                best_weight = w
            score = w * max(r, 1)
            if score > best_score:
                best_score = score
                best_reps = r
                best_set_weight = w
    return {
        "best_weight": best_weight,
        "best_reps": best_reps,
        "best_set_weight": best_set_weight,
    }


def format_pr_line(pr: dict) -> str:
    if pr.get("best_weight") is None:
        return ""
    return (
        f"PR: {pr['best_weight']:g} кг"
        + (
            f", лучший сет {pr['best_reps']}×{pr['best_set_weight']:g}"
            if pr.get("best_reps") is not None
            else ""
        )
    )


async def compare_line_for_exercise(
    session: AsyncSession,
    user_id: int,
    *,
    exercise_id: int | None,
    exercise_name: str,
) -> str:
    history = await _sessions_with_exercise(
        session,
        user_id,
        exercise_id=exercise_id,
        exercise_name=exercise_name,
        limit=2,
    )
    if len(history) < 1:
        return ""
    last = history[0]
    last_rows = [
        s
        for s in last.sets
        if (exercise_id and s.exercise_id == exercise_id)
        or name_key(s.exercise_name) == name_key(exercise_name)
    ]
    if not last_rows:
        return ""
    text = (
        f"Прошлый раз ({ui.format_user_date(last.session_date)}): "
        f"{format_session_exercise(last_rows)}"
    )
    if len(history) >= 2:
        prev = history[1]
        prev_rows = [
            s
            for s in prev.sets
            if (exercise_id and s.exercise_id == exercise_id)
            or name_key(s.exercise_name) == name_key(exercise_name)
        ]
        if prev_rows:
            cur_w = max(s.weight for s in last_rows)
            old_w = max(s.weight for s in prev_rows)
            cur_v = sum(s.volume for s in last_rows)
            old_v = sum(s.volume for s in prev_rows)
            if cur_w > old_w or cur_v > old_v:
                text += " ↑ лучше предыдущего"
            elif cur_w < old_w and cur_v < old_v:
                text += " ↓ слабее предыдущего"
            else:
                text += " ≈ как раньше"
    return text


async def format_athlete_week(session: AsyncSession, user_id: int, *, days: int = 7) -> str:
    from datetime import date, timedelta

    from app.db.models import User

    user = await session.get(User, user_id)
    label = athlete_label(user) if user else f"#{user_id}"
    since = date.today() - timedelta(days=days - 1)
    result = await session.execute(
        select(WorkoutSession)
        .where(
            WorkoutSession.user_id == user_id,
            WorkoutSession.status == SessionStatus.finished,
            WorkoutSession.session_date >= since,
        )
        .options(
            selectinload(WorkoutSession.template),
            selectinload(WorkoutSession.sets),
        )
        .order_by(WorkoutSession.session_date.desc(), WorkoutSession.id.desc())
    )
    sessions = list(result.scalars().all())
    lines = [f"{ui.ICO_HISTORY} Неделя · {label}", f"Сессий: {len(sessions)}"]
    if not sessions:
        lines.append("Пока пусто.")
        return "\n".join(lines)
    total_vol = sum(sum(s.volume for s in ws.sets) for ws in sessions)
    lines.append(f"Объём: {total_vol:g} кг·повт")
    lines.append("")
    stuck: list[str] = []
    for ws in sessions:
        title = ws.template.name if ws.template else "Тренировка"
        lines.append(f"{ui.format_user_date(ws.session_date)} · {title}")
        grouped: dict[str, list] = defaultdict(list)
        for s in ws.sets:
            grouped[s.exercise_name].append(s)
        for name, rows in grouped.items():
            mains = [r for r in rows if (r.drop_index or 0) == 0] or rows
            top = max(mains, key=lambda r: float(r.weight))
            lines.append(f"  {name}: {format_session_exercise(rows)} (раб. {top.weight:g})")
            if rows[-1].difficulty.value in ("hard", "failure"):
                stuck.append(f"• {name} ({ui.format_user_date(ws.session_date)})")
    if stuck:
        lines.append("")
        lines.append("Тяжело / отказ:")
        lines.extend(stuck)
    return "\n".join(lines)


async def list_athletes_missing_today(
    session: AsyncSession,
    day,
    *,
    template_id: int | None,
) -> list:
    from app.db.models import User

    users = list(
        (
            await session.execute(select(User).where(User.onboarding_done.is_(True)))
        ).scalars().all()
    )
    q = select(WorkoutSession.user_id).where(
        WorkoutSession.session_date == day,
        WorkoutSession.status == SessionStatus.finished,
    )
    if template_id is not None:
        q = q.where(WorkoutSession.template_id == template_id)
    finished_ids = set((await session.execute(q)).scalars().all())
    return [u for u in users if u.id not in finished_ids]


async def format_missing_today(session: AsyncSession, day, template_id: int | None) -> str:
    missing = await list_athletes_missing_today(session, day, template_id=template_id)
    if not missing:
        return f"{ui.ICO_DONE} Все онборждённые уже залогировали сегодня. Красавцы."
    lines = [f"{ui.ICO_FIRE} Ещё не залогировали сегодня:"]
    for u in missing:
        lines.append(f"• {u.short_code} · {u.display_name}")
    return "\n".join(lines)
