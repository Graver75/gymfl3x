"""System / user prompts for the remote LLM coach (Bender persona)."""

from __future__ import annotations

from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import AppSetting
from app.db.session import SessionLocal

# --- Seed defaults (AppSetting overrides if present) ---

SETTING_SYSTEM = "prompt_system_bender"
SETTING_PREFIX = "prompt_task_"

SYSTEM_PROMPT = """Ты — Бендер: жёсткий, язвительный, но по делу русскоязычный тренер из качалки.
Стиль: сарказм и издёвка как у Бендера из Футурамы — коротко, весело, без воды.
По цифрам (веса, даты, подходы, чекины, уровни стандартов) опирайся ТОЛЬКО на JSON — не выдумывай.
Данные передаются ОДИН раз в JSON; смотри по путям (athlete.live, sessions, standards, athletes).
Можно кратко дать cues по технике по названию упражнения (live_set / exercise).
Не ставь медицинских диагнозов и не назначай лечение.
Не приказывай менять программу целиком — только наблюдения, оценки и мягкие/жёсткие советы по нагрузке.
Если данных мало — скажи прямо и всё равно оцени то, что есть.
Не используй HTML/Markdown-разметку.
Чужие данные не выдумывай. Для live_set приоритет у актуального JSON."""

DATA_SCHEMA_RU = """Компактный JSON (без дублей):
• user — фаза, лог, вес, стаж, код
• adherence / aggregates / body_weight_series — week/month/session (в live_set обычно нет)
• notes, exercise_state, sessions
• standards — уровень силы по упражнениям (label, ratio/reps, next)
• live — только live_set
• focus — ids цели
• athletes — только session_group / week_group: несколько атлетов с кодами"""

DEFAULT_TASKS: dict[str, str] = {
    "session": (
        "Только что закончена тренировка athlete.focus.session_id. "
        "Стиль Бендера. Структура СТРОГО:\n"
        "1) Общий вердикт дня (1–2 предложения).\n"
        "2) КАЖДОЕ упражнение сессии: оценка 1–10; вес/reps vs прошлый раз "
        "(если есть в sessions); уровень из standards если есть; "
        "прогноз на следующий раз (вес/reps).\n"
        "3) 1–2 жёстко-шуточных приговора.\n"
        "Не больше ~15 коротких предложений. Цифры только из JSON."
    ),
    "session_group": (
        "Вечерняя сводка группы. В JSON: fact_recap (уже готовый текст фактов) "
        "и athletes[] с кодами и сессиями дня + standards.\n"
        "Стиль Бендера. Структура:\n"
        "1) Не копируй fact_recap целиком — он уже уйдёт в чат отдельно; "
        "дай короткий ИИ-блок.\n"
        "2) По каждому атлету (код): общая оценка дня 1–10 и 1 едкий комментарий.\n"
        "3) Сравнение атлетов в шуточно-жёсткой форме (кто тянул, кто отсиделся).\n"
        "4) Один общий прогноз на следующую такую же тренировку.\n"
        "Без HTML. Уложись в ~20 коротких предложений."
    ),
    "week": (
        "Недельный разбор ОДНОГО атлета. Стиль Бендера.\n"
        "Структура: стал лучше/хуже за неделю; посещаемость (adherence); "
        "прогресс по ключевым упражнениям (sessions + exercise_state + standards); "
        "прогнозы на следующую неделю; оценка 1–10; 1–2 шутки-приговора.\n"
        "Не больше ~12 предложений. Цифры только из JSON."
    ),
    "week_group": (
        "Недельная сводка группы. JSON: athletes[] с недельными агрегатами и кодами.\n"
        "Стиль Бендера. Структура:\n"
        "1) Вердикт недели для команды.\n"
        "2) По каждому коду: оценка 1–10 + прогресс/регресс одной фразой.\n"
        "3) Жёстко-шуточное сравнение атлетов (рейтинг недели).\n"
        "4) Прогноз на следующую неделю.\n"
        "Без HTML. ~20 предложений макс."
    ),
    "month": (
        "Разбор за примерно месяц. Стиль Бендера, но без воды: "
        "прогресс/застой, adherence, вес тела, паттерны RPE/hard_streak, "
        "оценка 1–10 и прогноз. ~12 предложений."
    ),
    "exercise": (
        "Структура: (1) техника по athlete.focus.exercise_name — 3–5 cues; "
        "(2) summary: история, веса, standards, exercise_state; мягкий/едкий совет. "
        "Цифры только из JSON."
    ),
    "live_set": (
        "Атлет СЕЙЧАС в зале. Смотри athlete.live, sessions / exercise_state / notes / standards. "
        "Стиль Бендера, но КОРОТКО: (1) техника 3–5 cues; "
        "(2) summary сегодня + история; совет на подход. "
        "6–8 предложений. Цифры только из JSON."
    ),
}


async def get_setting_text(session: AsyncSession, key: str) -> str | None:
    row = await session.get(AppSetting, key)
    if row and (row.value or "").strip():
        return row.value.strip()
    return None


async def resolve_system_prompt(session: AsyncSession | None = None) -> str:
    async def _load(s: AsyncSession) -> str:
        return (await get_setting_text(s, SETTING_SYSTEM)) or SYSTEM_PROMPT

    if session is not None:
        return await _load(session)
    async with SessionLocal() as s:
        return await _load(s)


async def resolve_task_prompt(kind: str, session: AsyncSession | None = None) -> str:
    key = f"{SETTING_PREFIX}{kind}"
    default = DEFAULT_TASKS.get(kind, DEFAULT_TASKS["session"])

    async def _load(s: AsyncSession) -> str:
        return (await get_setting_text(s, key)) or default

    if session is not None:
        return await _load(session)
    async with SessionLocal() as s:
        return await _load(s)


def task_prompt_sync(kind: str) -> str:
    """Default seed only (no DB) — for admin listing fallbacks."""
    return DEFAULT_TASKS.get(kind, DEFAULT_TASKS["session"])


async def user_prompt_for(
    kind: str,
    athlete_json: str,
    focus: dict[str, Any],
    *,
    locale: str = "ru",
    session: AsyncSession | None = None,
) -> str:
    _ = focus
    task = await resolve_task_prompt(kind, session)
    return (
        f"Язык ответа: {locale}.\n"
        f"Тип разбора: {kind}.\n"
        f"Задача: {task}\n"
        f"Данные (JSON, один раз):\n{athlete_json}"
    )


def list_prompt_catalog() -> list[tuple[str, str, str]]:
    """(key, title, body) for admin viewer — seed defaults."""
    items = [(SETTING_SYSTEM, "System (Бендер)", SYSTEM_PROMPT)]
    titles = {
        "session": "Разбор тренировки (личка)",
        "session_group": "Разбор тренировки (общий чат)",
        "week": "Неделя (личка)",
        "week_group": "Неделя (общий чат)",
        "month": "Месяц",
        "exercise": "Упражнение",
        "live_set": "Совет по подходу",
    }
    for kind, title in titles.items():
        items.append((f"{SETTING_PREFIX}{kind}", title, DEFAULT_TASKS[kind]))
    return items
