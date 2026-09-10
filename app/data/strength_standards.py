"""Default strength standards (BW ratios or reps). Admin edits live in SQLite."""

from __future__ import annotations

from typing import Any

# Canonical name_key -> seed payload. Insert-only on boot (never overwrite admin edits).
# Levels: новичок, новичок+, средний, продвинутый, элита
# mode=ratio: thresholds are e1RM / bodyweight
# mode=reps: thresholds are raw reps (best set)

DEFAULT_ISOLATION_MALE = (0.25, 0.4, 0.55, 0.75, 0.95)
DEFAULT_ISOLATION_FEMALE = (0.15, 0.25, 0.4, 0.55, 0.7)

SEED: dict[str, dict[str, Any]] = {
    "верхняя тяга палка": {
        "name": "Верхняя тяга палка",
        "mode": "ratio",
        "male": (0.5, 0.75, 1.05, 1.35, 1.7),
        "female": (0.3, 0.5, 0.75, 1.0, 1.3),
    },
    "верхняя тяга узкий хват": {
        "name": "Верхняя тяга узкий хват",
        "mode": "ratio",
        "male": (0.45, 0.7, 1.0, 1.3, 1.65),
        "female": (0.28, 0.45, 0.7, 0.95, 1.25),
    },
    "тяга горизонтального блока": {
        "name": "Тяга горизонтального блока",
        "mode": "ratio",
        "male": (0.5, 0.8, 1.1, 1.45, 1.85),
        "female": (0.3, 0.55, 0.8, 1.1, 1.4),
    },
    "жим ногами": {
        "name": "Жим ногами",
        "mode": "ratio",
        "male": (1.5, 2.5, 3.5, 4.5, 5.5),
        "female": (1.0, 1.75, 2.5, 3.25, 4.0),
        "needs_review": True,
    },
    "hammer strength верхняя грудь": {
        "name": "Hammer Strength верхняя грудь",
        "mode": "ratio",
        "male": (0.5, 0.85, 1.15, 1.5, 1.85),
        "female": (0.25, 0.5, 0.75, 1.0, 1.25),
    },
    "hammer strength плечи": {
        "name": "Hammer Strength плечи",
        "mode": "ratio",
        "male": (0.3, 0.5, 0.7, 0.95, 1.2),
        "female": (0.18, 0.3, 0.45, 0.6, 0.8),
    },
    "хаммер": {
        "name": "Хаммер",
        "mode": "ratio",
        "male": (0.45, 0.75, 1.05, 1.4, 1.75),
        "female": (0.28, 0.5, 0.75, 1.0, 1.3),
        "needs_review": True,
    },
    "хаммер плечи": {
        "name": "Хаммер плечи",
        "mode": "ratio",
        "male": (0.3, 0.5, 0.7, 0.95, 1.2),
        "female": (0.18, 0.3, 0.45, 0.6, 0.8),
    },
    "бабочка": {
        "name": "Бабочка",
        "mode": "ratio",
        "male": DEFAULT_ISOLATION_MALE,
        "female": DEFAULT_ISOLATION_FEMALE,
    },
    "обабочка": {
        "name": "Обабочка",
        "mode": "ratio",
        "male": DEFAULT_ISOLATION_MALE,
        "female": DEFAULT_ISOLATION_FEMALE,
        "needs_review": True,
    },
    "бицепс": {
        "name": "Бицепс",
        "mode": "ratio",
        "male": (0.2, 0.35, 0.5, 0.65, 0.85),
        "female": (0.12, 0.22, 0.35, 0.5, 0.65),
    },
    "молоточки": {
        "name": "Молоточки",
        "mode": "ratio",
        "male": (0.2, 0.35, 0.5, 0.65, 0.85),
        "female": (0.12, 0.22, 0.35, 0.5, 0.65),
    },
    "трицепс канат": {
        "name": "Трицепс канат",
        "mode": "ratio",
        "male": (0.2, 0.35, 0.5, 0.7, 0.9),
        "female": (0.12, 0.22, 0.35, 0.5, 0.7),
    },
    "трицепс палка": {
        "name": "Трицепс палка",
        "mode": "ratio",
        "male": (0.2, 0.35, 0.5, 0.7, 0.9),
        "female": (0.12, 0.22, 0.35, 0.5, 0.7),
    },
    "махи": {
        "name": "Махи",
        "mode": "ratio",
        "male": (0.1, 0.18, 0.28, 0.4, 0.55),
        "female": (0.08, 0.14, 0.22, 0.32, 0.45),
    },
    "сгибания ног": {
        "name": "Сгибания ног",
        "mode": "ratio",
        "male": (0.35, 0.55, 0.8, 1.05, 1.35),
        "female": (0.25, 0.4, 0.6, 0.85, 1.1),
    },
    "крикры": {
        "name": "Крикры",
        "mode": "ratio",
        "male": (0.5, 0.85, 1.2, 1.6, 2.0),
        "female": (0.35, 0.6, 0.9, 1.2, 1.55),
        "needs_review": True,
    },
    "икры": {
        "name": "Икры",
        "mode": "ratio",
        "male": (0.5, 0.85, 1.2, 1.6, 2.0),
        "female": (0.35, 0.6, 0.9, 1.2, 1.55),
    },
    "скручивания": {
        "name": "Скручивания",
        "mode": "reps",
        "male": (8, 15, 25, 40, 60),
        "female": (8, 15, 25, 40, 60),
    },
    "подъём ног к корпусу": {
        "name": "Подъём ног к корпусу",
        "mode": "reps",
        "male": (5, 10, 15, 20, 30),
        "female": (4, 8, 12, 18, 25),
    },
    "суперсет предплечья": {
        "name": "Суперсет предплечья",
        "mode": "ratio",
        "male": DEFAULT_ISOLATION_MALE,
        "female": DEFAULT_ISOLATION_FEMALE,
        "needs_review": True,
    },
}

# Resolve alternate spellings to a seed key (or existing standard key).
ALIASES: dict[str, str] = {
    "обабочка": "бабочка",
    "крикры": "икры",
    "hammer strength плечи": "хаммер плечи",
}
