"""Strength level gamification: e1RM / BW (or reps) vs editable standards."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Sequence

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.data.strength_standards import (
    ALIASES,
    DEFAULT_ISOLATION_FEMALE,
    DEFAULT_ISOLATION_MALE,
    SEED,
)
from app.db.models import (
    ExerciseArchive,
    ExerciseStrengthStandard,
    SessionStatus,
    TemplateExercise,
    User,
    WorkoutSession,
)
from app.services.archive import name_key

NUM_LEVELS = 10
# 1 ≈ child-easy … 10 ≈ super-athlete (fun gym ladder)
LEVEL_LABELS = (
    "Росток",  # 1 — почти детский вес
    "Новичок",  # 2 — Beginner (Strength Level)
    "Ученик",  # 3
    "Боец",  # 4 — Novice
    "Атлет",  # 5
    "Силач",  # 6 — Intermediate
    "Зверь",  # 7
    "Чемпион",  # 8 — Advanced
    "Титан",  # 9
    "Легенда",  # 10 — выше Elite
)
BELOW_LABEL = "до Ростка"
_MALE_ATTRS = tuple(f"male_t{i}" for i in range(1, NUM_LEVELS + 1))
_FEMALE_ATTRS = tuple(f"female_t{i}" for i in range(1, NUM_LEVELS + 1))


@dataclass
class LevelResult:
    exercise_name: str
    level_index: int | None  # 0..9 or None if below level 1
    level_label: str
    mode: str
    value: float  # ratio or reps
    e1rm: float | None
    body_weight: float | None
    next_label: str | None
    next_threshold: float | None
    next_e1rm_kg: float | None
    next_work_weight: float | None
    target_reps: int
    missing_standard: bool = False
    missing_bw: bool = False
    needs_review: bool = False


def epley_1rm(weight: float, reps: int) -> float:
    if weight <= 0:
        return 0.0
    r = max(int(reps), 1)
    if r == 1:
        return float(weight)
    return float(weight) * (1.0 + r / 30.0)


def work_weight_for_reps(e1rm: float, reps: int) -> float:
    """Inverse Epley: weight that yields ~e1rm at given reps."""
    r = max(int(reps), 1)
    if r == 1:
        return e1rm
    return e1rm / (1.0 + r / 30.0)


def best_set_from_parts(parts: Sequence[dict]) -> tuple[float, int, float]:
    """Return (weight, reps, e1rm) for the strongest logged set."""
    best_w, best_r, best_e = 0.0, 0, 0.0
    for p in parts:
        w = float(p.get("weight") or 0)
        r = int(p.get("reps") or 0)
        if w <= 0 and r <= 0:
            continue
        e = epley_1rm(w, r) if w > 0 else float(r)
        if e > best_e or (abs(e - best_e) < 1e-6 and w > best_w):
            best_w, best_r, best_e = w, r, e
    return best_w, best_r, best_e


def resolve_key(raw_name: str) -> str:
    key = name_key(raw_name)
    return ALIASES.get(key, key)


def thresholds_for(std: ExerciseStrengthStandard, sex: str) -> list[float]:
    attrs = _FEMALE_ATTRS if sex == "female" else _MALE_ATTRS
    return [float(getattr(std, a) or 0) for a in attrs]


def set_thresholds(std: ExerciseStrengthStandard, sex: str, values: Sequence[float]) -> None:
    v = [float(x) for x in values]
    if len(v) != NUM_LEVELS:
        raise ValueError("need_10")
    if any(v[i] >= v[i + 1] for i in range(NUM_LEVELS - 1)):
        raise ValueError("not_increasing")
    attrs = _FEMALE_ATTRS if sex == "female" else _MALE_ATTRS
    for attr, val in zip(attrs, v, strict=True):
        setattr(std, attr, val)
    std.needs_review = False


def level_index_for(value: float, thresholds: Sequence[float]) -> int | None:
    idx: int | None = None
    for i, t in enumerate(thresholds):
        if value + 1e-9 >= float(t):
            idx = i
        else:
            break
    return idx


def _apply_pair(std: ExerciseStrengthStandard, male: Sequence[float], female: Sequence[float]) -> None:
    for attr, val in zip(_MALE_ATTRS, male, strict=True):
        setattr(std, attr, float(val))
    for attr, val in zip(_FEMALE_ATTRS, female, strict=True):
        setattr(std, attr, float(val))


def _row_from_seed(key: str, payload: dict[str, Any]) -> ExerciseStrengthStandard:
    male = tuple(payload["male"])
    female = tuple(payload["female"])
    row = ExerciseStrengthStandard(
        name=str(payload.get("name") or key)[:128],
        name_key=key,
        mode=str(payload.get("mode") or "ratio"),
        needs_review=bool(payload.get("needs_review", False)),
    )
    _apply_pair(row, male, female)
    return row


def _default_isolation_row(name: str, key: str) -> ExerciseStrengthStandard:
    row = ExerciseStrengthStandard(
        name=name.strip()[:128],
        name_key=key,
        mode="ratio",
        needs_review=True,
    )
    _apply_pair(row, DEFAULT_ISOLATION_MALE, DEFAULT_ISOLATION_FEMALE)
    return row


async def ensure_standards(session: AsyncSession) -> int:
    """Insert seed rows for missing keys only. Returns number inserted."""
    existing = {
        r.name_key
        for r in (await session.execute(select(ExerciseStrengthStandard))).scalars().all()
    }
    added = 0
    for key, payload in SEED.items():
        if key in existing:
            continue
        session.add(_row_from_seed(key, payload))
        existing.add(key)
        added += 1
    if added:
        await session.commit()
    return added


async def upgrade_standards_to_v10(session: AsyncSession) -> int:
    """Fill t6..t10 / reseed from SL-based 10-level seeds when migrating from 5 levels."""
    rows = list((await session.execute(select(ExerciseStrengthStandard))).scalars().all())
    if not rows:
        return 0
    needs = any(getattr(r, "male_t10", None) is None for r in rows)
    if not needs:
        # Also upgrade if still looking like old 5-level seed (t6 equals default NULL filled as 0)
        # After ALTER, SQLite leaves new cols NULL → needs=True. If already filled, skip.
        return 0

    updated = 0
    for std in rows:
        payload = SEED.get(std.name_key)
        if payload:
            fresh = _row_from_seed(std.name_key, payload)
            std.name = fresh.name
            std.mode = fresh.mode
            _apply_pair(std, [getattr(fresh, a) for a in _MALE_ATTRS], [getattr(fresh, a) for a in _FEMALE_ATTRS])
            std.needs_review = fresh.needs_review
        else:
            # Expand old t1..t5 into 10 steps, or fall back to isolation defaults
            old_m = [float(getattr(std, f"male_t{i}") or 0) for i in range(1, 6)]
            old_f = [float(getattr(std, f"female_t{i}") or 0) for i in range(1, 6)]
            if all(x > 0 for x in old_m) and all(x > 0 for x in old_f):
                male = _expand_five_to_ten(old_m)
                female = _expand_five_to_ten(old_f)
                _apply_pair(std, male, female)
            else:
                _apply_pair(std, DEFAULT_ISOLATION_MALE, DEFAULT_ISOLATION_FEMALE)
                std.needs_review = True
        updated += 1
    if updated:
        await session.commit()
    return updated


def _expand_five_to_ten(five: Sequence[float]) -> list[float]:
    beg, nov, mid, adv, elite = (float(x) for x in five)
    vals = [
        round(beg * 0.55, 3),
        round(beg, 3),
        round((beg + nov) / 2, 3),
        round(nov, 3),
        round((nov + mid) / 2, 3),
        round(mid, 3),
        round((mid + adv) / 2, 3),
        round(adv, 3),
        round((adv + elite) / 2, 3),
        round(elite * 1.18, 3),
    ]
    out = [vals[0]]
    for v in vals[1:]:
        out.append(max(v, out[-1] + 0.01))
    return [round(x, 3) for x in out]


async def sync_standards_from_catalog(session: AsyncSession) -> int:
    """Create missing standards for archive + template names. Does not overwrite."""
    await ensure_standards(session)
    existing = {
        r.name_key: r
        for r in (await session.execute(select(ExerciseStrengthStandard))).scalars().all()
    }
    names: set[str] = set()
    for n in (await session.execute(select(ExerciseArchive.name))).scalars().all():
        if n:
            names.add(n)
    for n in (await session.execute(select(TemplateExercise.name))).scalars().all():
        if n:
            names.add(n)

    added = 0
    for raw in names:
        key = resolve_key(raw)
        if key in existing:
            continue
        if key in SEED:
            row = _row_from_seed(key, SEED[key])
        else:
            row = _default_isolation_row(raw, key)
        session.add(row)
        existing[key] = row
        added += 1
    if added:
        await session.commit()
    return added


async def reset_standard_to_seed(session: AsyncSession, std_id: int) -> ExerciseStrengthStandard | None:
    std = await session.get(ExerciseStrengthStandard, std_id)
    if not std:
        return None
    payload = SEED.get(std.name_key)
    if not payload:
        return None
    fresh = _row_from_seed(std.name_key, payload)
    std.name = fresh.name
    std.mode = fresh.mode
    _apply_pair(
        std,
        [getattr(fresh, a) for a in _MALE_ATTRS],
        [getattr(fresh, a) for a in _FEMALE_ATTRS],
    )
    std.needs_review = fresh.needs_review
    await session.commit()
    return std


async def get_standard(session: AsyncSession, exercise_name: str) -> ExerciseStrengthStandard | None:
    key = resolve_key(exercise_name)
    result = await session.execute(
        select(ExerciseStrengthStandard).where(ExerciseStrengthStandard.name_key == key)
    )
    return result.scalar_one_or_none()


def evaluate_level(
    *,
    exercise_name: str,
    std: ExerciseStrengthStandard | None,
    parts: Sequence[dict],
    body_weight: float | None,
    sex: str | None,
    target_reps: int = 10,
) -> LevelResult:
    sex_key = "female" if sex == "female" else "male"
    weight, reps, e1rm = best_set_from_parts(parts)
    if not std:
        return LevelResult(
            exercise_name=exercise_name,
            level_index=None,
            level_label="нет шкалы",
            mode="ratio",
            value=0.0,
            e1rm=e1rm if weight > 0 else None,
            body_weight=body_weight,
            next_label=None,
            next_threshold=None,
            next_e1rm_kg=None,
            next_work_weight=None,
            target_reps=target_reps,
            missing_standard=True,
        )

    if std.mode == "reps":
        value = float(max(reps, 0))
        if value <= 0:
            value = e1rm  # fallback if logged oddly
        thresholds = thresholds_for(std, sex_key)
        idx = level_index_for(value, thresholds)
        label = LEVEL_LABELS[idx] if idx is not None else BELOW_LABEL
        next_label = None
        next_th = None
        if idx is None:
            next_label, next_th = LEVEL_LABELS[0], thresholds[0]
        elif idx < NUM_LEVELS - 1:
            next_label, next_th = LEVEL_LABELS[idx + 1], thresholds[idx + 1]
        return LevelResult(
            exercise_name=exercise_name,
            level_index=idx,
            level_label=label,
            mode="reps",
            value=value,
            e1rm=None,
            body_weight=body_weight,
            next_label=next_label,
            next_threshold=next_th,
            next_e1rm_kg=None,
            next_work_weight=None,
            target_reps=target_reps,
            needs_review=bool(std.needs_review),
        )

    if not body_weight or body_weight <= 0:
        return LevelResult(
            exercise_name=exercise_name,
            level_index=None,
            level_label="нет веса тела",
            mode="ratio",
            value=0.0,
            e1rm=e1rm if weight > 0 else None,
            body_weight=None,
            next_label=None,
            next_threshold=None,
            next_e1rm_kg=None,
            next_work_weight=None,
            target_reps=target_reps,
            missing_bw=True,
            needs_review=bool(std.needs_review),
        )

    ratio = e1rm / float(body_weight) if e1rm > 0 else 0.0
    thresholds = thresholds_for(std, sex_key)
    idx = level_index_for(ratio, thresholds)
    label = LEVEL_LABELS[idx] if idx is not None else BELOW_LABEL
    next_label = None
    next_th = None
    next_e1rm = None
    next_work = None
    if idx is None:
        next_label, next_th = LEVEL_LABELS[0], thresholds[0]
    elif idx < NUM_LEVELS - 1:
        next_label, next_th = LEVEL_LABELS[idx + 1], thresholds[idx + 1]
    if next_th is not None:
        next_e1rm = float(next_th) * float(body_weight)
        next_work = work_weight_for_reps(next_e1rm, target_reps)
    return LevelResult(
        exercise_name=exercise_name,
        level_index=idx,
        level_label=label,
        mode="ratio",
        value=ratio,
        e1rm=e1rm,
        body_weight=float(body_weight),
        next_label=next_label,
        next_threshold=next_th,
        next_e1rm_kg=next_e1rm,
        next_work_weight=next_work,
        target_reps=target_reps,
        needs_review=bool(std.needs_review),
    )


def format_level_feedback(result: LevelResult) -> str:
    if result.missing_bw:
        return "Уровень силы: укажи вес тела (/weight), тогда посчитаю."
    if result.missing_standard:
        return (
            "Уровень силы: шкалы для этого упражнения пока нет. "
            "Админ может добавить в «Уровни силы»."
        )
    lines = [f"Уровень: <b>{result.level_label}</b>"]
    if result.mode == "reps":
        lines.append(f"Лучший подход: {result.value:g} повт.")
    else:
        e1 = f"{result.e1rm:g}" if result.e1rm else "?"
        lines.append(f"e1RM ~{e1} кг · {result.value:.2f}×BW")
    if result.next_label and result.next_threshold is not None:
        if result.mode == "reps":
            lines.append(
                f"Следующий (<b>{result.next_label}</b>): от {result.next_threshold:g} повт."
            )
        else:
            e1 = f"{result.next_e1rm_kg:g}" if result.next_e1rm_kg else "?"
            ww = f"{result.next_work_weight:g}" if result.next_work_weight else "?"
            lines.append(
                f"Следующий (<b>{result.next_label}</b>): e1RM ~{e1} кг "
                f"≈ ~{ww} кг на {result.target_reps} повт."
            )
    elif result.level_index == NUM_LEVELS - 1:
        lines.append(f"Ты на вершине шкалы — <b>{LEVEL_LABELS[-1]}</b>.")
    if result.needs_review:
        lines.append("<i>Шкала помечена «нужен review» в админке.</i>")
    return "\n".join(lines)


async def evaluate_logged_exercise(
    session: AsyncSession,
    user: User,
    *,
    exercise_name: str,
    parts: Sequence[dict],
    target_reps: int = 10,
) -> LevelResult:
    std = await get_standard(session, exercise_name)
    return evaluate_level(
        exercise_name=exercise_name,
        std=std,
        parts=parts,
        body_weight=user.body_weight,
        sex=user.sex,
        target_reps=target_reps,
    )


async def list_standards(session: AsyncSession) -> list[ExerciseStrengthStandard]:
    result = await session.execute(
        select(ExerciseStrengthStandard).order_by(ExerciseStrengthStandard.name.asc())
    )
    return list(result.scalars().all())


async def profile_progress_lines(session: AsyncSession, user: User) -> str:
    """Build readable progress from latest finished sets per exercise name."""
    result = await session.execute(
        select(WorkoutSession)
        .where(
            WorkoutSession.user_id == user.id,
            WorkoutSession.status == SessionStatus.finished,
        )
        .options(selectinload(WorkoutSession.sets))
        .order_by(WorkoutSession.session_date.desc(), WorkoutSession.id.desc())
        .limit(40)
    )
    sessions = list(result.scalars().all())

    by_name: dict[str, list[dict]] = {}
    for ws in sessions:
        for s in ws.sets:
            key = s.exercise_name or "?"
            if key in by_name and len(by_name[key]) >= 12:
                continue
            by_name.setdefault(key, []).append(
                {"weight": s.weight, "reps": s.reps, "set_number": s.set_number}
            )

    if not by_name:
        return (
            "Пока нет завершённых тренировок — прогресс появится после "
            "«Закончить тренировку»."
        )

    counts = {label: 0 for label in LEVEL_LABELS}
    counts[BELOW_LABEL] = 0
    counts["нет шкалы"] = 0
    lines: list[str] = []
    sex = user.sex or "male"
    for name in sorted(by_name.keys(), key=lambda s: s.lower()):
        parts = by_name[name]
        std = await get_standard(session, name)
        res = evaluate_level(
            exercise_name=name,
            std=std,
            parts=parts,
            body_weight=user.body_weight,
            sex=sex,
            target_reps=10,
        )
        if res.missing_standard:
            counts["нет шкалы"] += 1
            lines.append(f"• {name}: нет шкалы")
            continue
        if res.missing_bw:
            lines.append(f"• {name}: укажи вес тела")
            continue
        key = res.level_label if res.level_label in counts else BELOW_LABEL
        counts[key] = counts.get(key, 0) + 1
        if res.mode == "reps":
            detail = f"{res.value:g} повт."
        else:
            detail = f"{res.value:.2f}×BW"
        nxt = ""
        if res.next_label and res.next_threshold is not None:
            if res.mode == "reps":
                nxt = f" → {res.next_label} от {res.next_threshold:g}"
            else:
                ww = f"{res.next_work_weight:g}" if res.next_work_weight else "?"
                nxt = f" → {res.next_label} ≈{ww} кг×10"
        lines.append(f"• <b>{name}</b>: {res.level_label} ({detail}){nxt}")

    # Compact summary: only non-zero buckets
    summary_parts = [
        f"{lab} {counts[lab]}" for lab in (*LEVEL_LABELS, BELOW_LABEL) if counts.get(lab, 0)
    ]
    summary = " · ".join(summary_parts) if summary_parts else "нет данных"
    header = [
        "<b>Прогресс по упражнениям</b>",
        f"Пол для шкалы: <b>{'Ж' if sex == 'female' else 'М'}</b>",
        f"Сводка: {summary}",
        "",
    ]
    return "\n".join(header + lines)
