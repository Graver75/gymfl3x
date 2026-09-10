"""Default strength standards (BW ratios or reps). Admin edits live in SQLite.

10 levels: 1 = very easy / child-like, 10 = super-athlete.
Anchors from Strength Level bodyweight ratios (Beginner→Elite),
then expanded: L1 below Beginner, L10 above Elite.
Source: https://strengthlevel.com (lat pulldown, seated row, etc.)
"""

from __future__ import annotations

from typing import Any


def _expand10(beg: float, nov: float, mid: float, adv: float, elite: float) -> tuple[float, ...]:
    """5 SL anchors → 10 strictly increasing thresholds."""
    vals = (
        round(beg * 0.55, 3),  # 1 child / very light
        round(beg, 3),  # 2 beginner
        round((beg + nov) / 2, 3),  # 3
        round(nov, 3),  # 4 novice
        round((nov + mid) / 2, 3),  # 5
        round(mid, 3),  # 6 intermediate
        round((mid + adv) / 2, 3),  # 7
        round(adv, 3),  # 8 advanced
        round((adv + elite) / 2, 3),  # 9
        round(elite * 1.18, 3),  # 10 super-athlete
    )
    out = [vals[0]]
    for v in vals[1:]:
        out.append(max(v, out[-1] + 0.01))
    return tuple(round(x, 3) for x in out)


# Strength Level lat pulldown: M 0.50/0.75/1.00/1.50/1.75, F 0.35/0.50/0.75/0.95/1.25
LAT_M = _expand10(0.50, 0.75, 1.00, 1.50, 1.75)
LAT_F = _expand10(0.35, 0.50, 0.75, 0.95, 1.25)
ROW_M = _expand10(0.50, 0.75, 1.00, 1.50, 1.75)
ROW_F = _expand10(0.35, 0.50, 0.75, 1.00, 1.30)
CURL_LEG_M = _expand10(0.50, 0.75, 1.00, 1.50, 2.00)
CURL_LEG_F = _expand10(0.30, 0.50, 0.75, 1.05, 1.40)
BENCH_M = _expand10(0.50, 0.75, 1.15, 1.50, 1.85)
BENCH_F = _expand10(0.25, 0.50, 0.75, 1.00, 1.25)
OHP_M = _expand10(0.30, 0.45, 0.65, 0.90, 1.15)
OHP_F = _expand10(0.18, 0.30, 0.45, 0.60, 0.80)
ISO_M = _expand10(0.15, 0.25, 0.40, 0.55, 0.75)
ISO_F = _expand10(0.10, 0.18, 0.28, 0.40, 0.55)
RAISE_M = _expand10(0.08, 0.14, 0.22, 0.32, 0.45)
RAISE_F = _expand10(0.06, 0.10, 0.16, 0.24, 0.35)
LEGPRESS_M = _expand10(1.20, 2.00, 3.00, 4.20, 5.50)
LEGPRESS_F = _expand10(0.80, 1.40, 2.20, 3.20, 4.20)
CALF_M = _expand10(0.40, 0.70, 1.10, 1.50, 2.00)
CALF_F = _expand10(0.30, 0.50, 0.80, 1.15, 1.55)
CRUNCH_M = _expand10(5, 10, 18, 30, 50)
CRUNCH_F = _expand10(5, 10, 18, 30, 50)
LEGLIFT_M = _expand10(3, 6, 10, 16, 25)
LEGLIFT_F = _expand10(2, 5, 8, 14, 22)

DEFAULT_ISOLATION_MALE = ISO_M
DEFAULT_ISOLATION_FEMALE = ISO_F

SEED: dict[str, dict[str, Any]] = {
    "верхняя тяга палка": {
        "name": "Верхняя тяга палка",
        "mode": "ratio",
        "male": LAT_M,
        "female": LAT_F,
    },
    "верхняя тяга узкий хват": {
        "name": "Верхняя тяга узкий хват",
        "mode": "ratio",
        "male": tuple(round(x * 0.95, 3) for x in LAT_M),
        "female": tuple(round(x * 0.95, 3) for x in LAT_F),
    },
    "тяга горизонтального блока": {
        "name": "Тяга горизонтального блока",
        "mode": "ratio",
        "male": ROW_M,
        "female": ROW_F,
    },
    "жим ногами": {
        "name": "Жим ногами",
        "mode": "ratio",
        "male": LEGPRESS_M,
        "female": LEGPRESS_F,
        "needs_review": True,
    },
    "hammer strength верхняя грудь": {
        "name": "Hammer Strength верхняя грудь",
        "mode": "ratio",
        "male": BENCH_M,
        "female": BENCH_F,
    },
    "hammer strength плечи": {
        "name": "Hammer Strength плечи",
        "mode": "ratio",
        "male": OHP_M,
        "female": OHP_F,
    },
    "хаммер": {
        "name": "Хаммер",
        "mode": "ratio",
        "male": BENCH_M,
        "female": BENCH_F,
        "needs_review": True,
    },
    "хаммер плечи": {
        "name": "Хаммер плечи",
        "mode": "ratio",
        "male": OHP_M,
        "female": OHP_F,
    },
    "бабочка": {
        "name": "Бабочка",
        "mode": "ratio",
        "male": ISO_M,
        "female": ISO_F,
    },
    "обабочка": {
        "name": "Обабочка",
        "mode": "ratio",
        "male": ISO_M,
        "female": ISO_F,
        "needs_review": True,
    },
    "бицепс": {
        "name": "Бицепс",
        "mode": "ratio",
        "male": ISO_M,
        "female": ISO_F,
    },
    "молоточки": {
        "name": "Молоточки",
        "mode": "ratio",
        "male": ISO_M,
        "female": ISO_F,
    },
    "трицепс канат": {
        "name": "Трицепс канат",
        "mode": "ratio",
        "male": ISO_M,
        "female": ISO_F,
    },
    "трицепс палка": {
        "name": "Трицепс палка",
        "mode": "ratio",
        "male": ISO_M,
        "female": ISO_F,
    },
    "махи": {
        "name": "Махи",
        "mode": "ratio",
        "male": RAISE_M,
        "female": RAISE_F,
    },
    "сгибания ног": {
        "name": "Сгибания ног",
        "mode": "ratio",
        "male": CURL_LEG_M,
        "female": CURL_LEG_F,
    },
    "крикры": {
        "name": "Крикры",
        "mode": "ratio",
        "male": CALF_M,
        "female": CALF_F,
        "needs_review": True,
    },
    "икры": {
        "name": "Икры",
        "mode": "ratio",
        "male": CALF_M,
        "female": CALF_F,
    },
    "скручивания": {
        "name": "Скручивания",
        "mode": "reps",
        "male": CRUNCH_M,
        "female": CRUNCH_F,
    },
    "подъём ног к корпусу": {
        "name": "Подъём ног к корпусу",
        "mode": "reps",
        "male": LEGLIFT_M,
        "female": LEGLIFT_F,
    },
    "суперсет предплечья": {
        "name": "Суперсет предплечья",
        "mode": "ratio",
        "male": ISO_M,
        "female": ISO_F,
        "needs_review": True,
    },
}

ALIASES: dict[str, str] = {
    "обабочка": "бабочка",
    "крикры": "икры",
    "hammer strength плечи": "хаммер плечи",
}
