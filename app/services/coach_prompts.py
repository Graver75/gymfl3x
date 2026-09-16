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
По цифрам (веса, даты, подходы, чекины) опирайся ТОЛЬКО на JSON — не выдумывай.
Данные передаются ОДИН раз в JSON; смотри по путям (athlete.live, sessions, athletes).
Можно кратко дать cues по технике по названию упражнения и machine_name / m (тренажёр), если указан (live_set / exercise).
В JSON НЕТ секунд отдыха между подходами — не ссылайся на «факт отдыха из данных».
Но в советах МОЖНО и НУЖНО рекомендовать отдых (например «90–120 с между подходами») как тренерский ориентир.
Не ставь медицинских диагнозов и не назначай лечение.
Не приказывай менять программу целиком — только наблюдения, оценки и мягкие/жёсткие советы по нагрузке.
НЕ используй и НЕ выдумывай шкалы/уровни силы (Новичок, Ростки, Профи и т.п.) — их нет в данных и они ошибочны.
Поле user.phase — только фаза прогрессии нагрузок (медовый/средний/плато), не «уровень силы».
Если данных мало — скажи прямо и всё равно оцени то, что есть.
Не используй HTML/Markdown-разметку.
Чужие данные не выдумывай. Для live_set приоритет у актуального JSON."""

DATA_SCHEMA_RU = """Компактный JSON (без дублей):
• user — фаза, лог, вес, рост height_cm, стаж, возраст, пол, код (без уровней силы)
• adherence / aggregates / body_weight_series — week/month/session (в live_set обычно нет)
• schedule — только week/week_group/week_plan: шаблоны пн–вс
• notes, exercise_state (m = тренажёр), sessions
• sessions: sets_n = рабочие подходы (уник. упражнение+номер); parts_n = все куски лога с дропами;
  свежие сессии — by_ex с полными kg/reps; старые за год — компакт (date/tpl/vol/sets_n/parts_n/top)
• live — только live_set (machine_name; week_plan текущего упражнения если есть)
• focus — ids цели + machine_name
• target_exercises — только week_plan: id/name/machine_name/target_sets…
• athletes — только session_group / week_group: несколько атлетов с кодами
• window_days у week* может быть до 365 — история за год; фокус вердикта — целевая/текущая неделя
• отдыха между подходами в данных нет — рекомендуй отдых сам в тексте совета
• machine_name / m — конкретный тренажёр; если указан, cues и советы под него"""

DEFAULT_TASKS: dict[str, str] = {
    "session": (
        "Только что закончена тренировка athlete.focus.session_id. "
        "Стиль Бендера. Структура СТРОГО:\n"
        "1) Общий вердикт дня (1–2 предложения).\n"
        "2) КАЖДОЕ упражнение сессии: оценка 1–10; вес/reps vs прошлый раз "
        "(если есть в sessions); прогноз на следующий раз (вес/reps).\n"
        "3) 1–2 жёстко-шуточных приговора.\n"
        "Без уровней силы. Не больше ~15 коротких предложений. Цифры только из JSON."
    ),
    "session_group": (
        "Вечерняя сводка группы. В JSON: fact_recap (уже готовый текст фактов) "
        "и athletes[] с кодами и сессиями дня.\n"
        "Стиль Бендера. Структура:\n"
        "1) Не копируй fact_recap целиком — он уже уйдёт в чат отдельно; "
        "дай короткий ИИ-блок.\n"
        "2) По каждому атлету (код): общая оценка дня 1–10 и 1 едкий комментарий.\n"
        "3) Сравнение атлетов в шуточно-жёсткой форме (кто тянул, кто отсиделся).\n"
        "4) Один общий прогноз на следующую такую же тренировку.\n"
        "Без уровней силы. Без HTML. Уложись в ~20 коротких предложений."
    ),
    "week": (
        "Недельный разбор ОДНОГО атлета. Стиль Бендера.\n"
        "В JSON sessions может быть история до ~года (window_days); "
        "фокус вердикта — ТЕКУЩАЯ/последняя неделя, год — только для тренда.\n"
        "sets_n = рабочие подходы; parts_n = куски лога с дропами — не путай.\n"
        "Структура: стал лучше/хуже за неделю; посещаемость (adherence); "
        "прогресс по ключевым упражнениям (sessions + exercise_state); "
        "прогнозы на следующую неделю; оценка 1–10; 1–2 шутки-приговора.\n"
        "Без уровней силы. Не больше ~12 предложений. Цифры только из JSON."
    ),
    "week_group": (
        "Недельная сводка группы. JSON: athletes[] с недельными агрегатами и кодами.\n"
        "У каждого атлета sessions может покрывать до ~года; "
        "фокус разбора — ТЕКУЩАЯ неделя / свежие даты; год — тренд и частота.\n"
        "sets_n = рабочие подходы; parts_n = с дропами — не называй parts_n «подходами на спину».\n"
        "Стиль Бендера — едкий, язвительный, жёстко-шуточный. Структура СТРОГО:\n"
        "1) Вердикт недели для команды (2–4 предложения).\n"
        "2) По кодам — ПОДРОБНО и ЖЁСТЧЕ: для КАЖДОГО кода оценка 1–10; "
        "конкретика по упражнениям/весам/reps/RPE/частоте из JSON; "
        "едкий разбор (не одна фраза — несколько предложений на человека).\n"
        "3) Рейтинг недели — жёстко-шуточное сравнение атлетов.\n"
        "4) Прогноз на следующую неделю — по кодам, что делать.\n"
        "Без уровней силы. Без HTML. Цифры только из JSON."
    ),
    "week_plan": (
        "СКРЫТЫЙ job: персональный план на целевую неделю (week_start). "
        "Ответ СТРОГО один JSON (без markdown, без текста вокруг).\n"
        "Схема: {\"exercises\":[{\"exercise_id\":int,\"advice\":str,"
        "\"sets\":[{\"n\":int,\"kg\":number,\"reps\":int,\"rpe\":int}]}]}.\n"
        "Покрывай ВСЕ exercise_id из target_exercises. Число подходов ≈ target_sets.\n"
        "История sessions может быть до ~года — используй для тренда весов; "
        "цифры плана — под целевую неделю и свежие рабочие веса.\n"
        "sets_n = рабочие подходы; parts_n = с дропами.\n"
        "advice: стиль Бендера (едкий, язвительный) — РОВНО 3–4 предложения; "
        "если у упражнения есть machine_name — ОБЯЗАТЕЛЬНО учти тренажёр "
        "(посадка, траектория, рычаги/рукояти) в cues/акценте; "
        "объясни ПОЧЕМУ такие kg/reps/rpe + рабочий акцент "
        "(техника/темп/отдых между подходами — рекомендуй секунды сам, "
        "в данных rest_sec нет); ОБЯЗАТЕЛЬНАЯ согласованность с sets[] "
        "(нельзя «полная жесть» при низком RPE и наоборот); "
        "если называешь цифры kg/reps/rpe — только те же, что в sets.\n"
        "Цифры нагрузки только из JSON. Без уровней силы."
    ),
    "month": (
        "Разбор за примерно месяц. Стиль Бендера, но без воды: "
        "прогресс/застой, adherence, вес тела, паттерны RPE/hard_streak, "
        "оценка 1–10 и прогноз. Без уровней силы. ~12 предложений."
    ),
    "exercise": (
        "Структура: (1) техника по athlete.focus.exercise_name "
        "(учти machine_name / m — конкретный тренажёр) — 3–5 cues; "
        "(2) summary: история, веса, exercise_state; мягкий/едкий совет; "
        "по желанию рекомендуй отдых между подходами (в данных его нет).\n"
        "Без уровней силы. Цифры нагрузки только из JSON."
    ),
    "live_set": (
        "Атлет СЕЙЧАС в зале. Смотри athlete.live (machine_name, logged_sets, "
        "week_plan если есть). Стиль Бендера, КОРОТКО.\n"
        "Недельный advice на карточке УЖЕ показан — НЕ пересказывай и НЕ копируй "
        "текст live.week_plan.advice; не дублируй те же формулировки.\n"
        "(1) техника 3–5 cues на ЭТОТ подход сейчас — если live.machine_name "
        "или focus.machine_name есть, cues ПОД ЭТОТ тренажёр (не общие); "
        "(2) корректировка по уже залогированным сетам сессии; "
        "(3) при необходимости дай ориентир по отдыху до следующего подхода "
        "(секунд в JSON нет — рекомендуй сам).\n"
        "цифры плана (kg/reps/rpe) можно кратко опереться, без повтора advice.\n"
        "Без уровней силы. 6–8 предложений. Цифры нагрузки только из JSON."
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
        "week_plan": "Скрытый: прогноз недели",
        "month": "Месяц",
        "exercise": "Упражнение",
        "live_set": "Совет по подходу",
    }
    for kind, title in titles.items():
        items.append((f"{SETTING_PREFIX}{kind}", title, DEFAULT_TASKS[kind]))
    return items


def catalog_kind(key: str) -> str | None:
    """Map catalog setting key → coach kind, or None for system."""
    if key == SETTING_SYSTEM:
        return None
    if key.startswith(SETTING_PREFIX):
        return key[len(SETTING_PREFIX) :]
    return None


# Brief + full docs: what JSON is injected with each prompt kind
_KIND_PAYLOAD_BRIEF: dict[str, str] = {
    "__system__": (
        "В system уходит только текст персоны Бендера. "
        "JSON атлета — в user-сообщении вместе с задачей и DATA_SCHEMA_RU."
    ),
    "session": (
        "Один атлет · окно ~14 дн · до 8 сессий. "
        "user (фаза/вес/рост/пол/возраст/стаж/код), sessions (focus + прошлые, "
        "полные сеты на focus), exercise_state, notes, adherence, aggregates, "
        "BW-серия, focus.session_id. Без live / athletes / target_exercises."
    ),
    "session_group": (
        "Общий чат дня: fact_recap + athletes[] (код/имя + урезанный session-контекст "
        "каждого). Окно ~14 дн. Без week schedule / target_exercises."
    ),
    "week": (
        "Один атлет · окно до 365 дн · до 80 сессий (свежие с полным by_ex, "
        "старые компакт). user+height, adherence, aggregates, BW(~52), notes, "
        "все exercise_state с весами, schedule пн–вс. Без live / athletes."
    ),
    "week_group": (
        "athletes[] — у каждого тот же week-контекст (год истории). "
        "Фокус вердикта — текущая неделя. Без target_exercises."
    ),
    "week_plan": (
        "Как week (год) + target_exercises[] (id/name/machine/targets) "
        "и week_start. Ответ — JSON плана, не текст в чат."
    ),
    "month": (
        "Один атлет · ~45 дн · до 18 сессий. user, sessions (by_ex), "
        "exercise_state (в основном hard_streak/note), notes, adherence, "
        "aggregates, BW. Без live / schedule / athletes."
    ),
    "exercise": (
        "Один атлет · ~45 дн · сессии только с этим упражнением (полные сеты). "
        "focus.exercise_id/name + machine_name, exercise_state этого движения, "
        "notes. Без live."
    ),
    "live_set": (
        "Один атлет · ~45 дн по упражнению + athlete.live: machine_name, "
        "current_set, draft kg/reps, logged_sets, week_plan (advice+sets) "
        "для анти-дубля. Без adherence/aggregates/BW в payload."
    ),
}

_KIND_PAYLOAD_FULL: dict[str, str] = {
    "__system__": (
        "System prompt (Бендер)\n"
        "─────────────────────\n"
        "Не содержит JSON атлета.\n\n"
        "User-сообщение (собирается в nn_client):\n"
        "· Язык ответа\n"
        "· Тип разбора (kind)\n"
        "· Задача = prompt_task_<kind>\n"
        "· Данные (JSON, один раз) = athlete payload\n\n"
        "Общая схема полей (DATA_SCHEMA_RU):\n"
        f"{DATA_SCHEMA_RU}"
    ),
    "session": (
        "kind=session\n"
        "window_days=14 · max_sessions=8\n\n"
        "Корни JSON:\n"
        "· user: phase, log_level, bw, height_cm, exp_m, code, sex, age\n"
        "· focus: session_id (+ machine если есть)\n"
        "· sessions[]: id, date, tpl, dur, checkin, vol, sets_n, parts_n;\n"
        "  focus/недавние same-tpl — полные sets[] (ex,n,d,reps,kg,diff,rpe,m);\n"
        "  остальные — компакт\n"
        "· exercise_state[]: ex_id, ex, m, ww, sw, last_reps/sets, diff, hard_streak, note\n"
        "· notes[]: ex, text, cleared, at\n"
        "· adherence, aggregates (sessions_n, total_vol, avg_rpe, bw_delta)\n"
        "· body_weight_series[]\n\n"
        "Нет: live, athletes, target_exercises, schedule, rest_sec, уровни силы"
    ),
    "session_group": (
        "kind=session_group\n"
        "Обёртка: date, template, hashtag, fact_recap, athletes[]\n\n"
        "Каждый athlete:\n"
        "· code, name\n"
        "· user / sessions / exercise_state / focus — как session\n"
        "  (focus = сегодняшняя сессия если была)\n\n"
        "Нет: week schedule, target_exercises, live"
    ),
    "week": (
        "kind=week\n"
        "window_days=365 · max_sessions=80 · recent_full≈16\n\n"
        "Корни:\n"
        "· user (+ height_cm)\n"
        "· schedule[]: wd, date, tpl, tpl_id (пн–вс)\n"
        "· sessions[]:\n"
        "  recent — by_ex с полными kg/reps, sets_n (рабочие), parts_n (с дропами),\n"
        "  checkin, machine m, diff, rpe_avg\n"
        "  older — date, tpl, vol, sets_n, parts_n, top[2] по объёму\n"
        "· exercise_state — все с ww/sw/streak/note (до ~60)\n"
        "· notes (до ~40), adherence, aggregates, body_weight_series (~52 точек)\n\n"
        "Нет: live, athletes, target_exercises"
    ),
    "week_group": (
        "kind=week_group\n"
        "athletes[] — у каждого полный week-payload (год истории).\n"
        "Фокус текста — текущая неделя; год для тренда.\n"
        "sets_n ≠ parts_n.\n\n"
        "Нет: target_exercises, live"
    ),
    "week_plan": (
        "kind=week_plan\n"
        "База как week (365д) +\n"
        "· week_start\n"
        "· target_exercises[]: exercise_id, name, machine_name,\n"
        "  target_sets, target_reps_min/max, weight_step\n"
        "· chunk (если дробление)\n\n"
        "Ответ модели: JSON {exercises:[{exercise_id, advice, sets[{n,kg,reps,rpe}]}]}"
    ),
    "month": (
        "kind=month\n"
        "window_days=45 · max_sessions=18\n\n"
        "user, sessions (by_ex), notes, adherence, aggregates, BW,\n"
        "exercise_state в основном с hard_streak или note.\n\n"
        "Нет: live, schedule, athletes, target_exercises"
    ),
    "exercise": (
        "kind=exercise\n"
        "window_days=45\n\n"
        "· focus.exercise_id / exercise_name / machine_name\n"
        "· sessions — только с этим упражнением, полные sets[]\n"
        "· exercise_state / notes — только это движение\n"
        "· user, adherence, aggregates, BW\n\n"
        "Нет: live, athletes, target_exercises"
    ),
    "live_set": (
        "kind=live_set\n"
        "window_days=45 (история упражнения) +\n\n"
        "athlete.live:\n"
        "· exercise_id/name, session_id, screen, current_set, sets_done, drop_index\n"
        "· target, draft_weight/reps\n"
        "· logged_sets[], saved_sets_in_session?\n"
        "· machine_name\n"
        "· week_plan? {week_start, advice, sets[]} — уже на карточке, не дублировать\n\n"
        "focus + machine_name\n"
        "Нет в live_set: adherence, aggregates, body_weight_series"
    ),
}


def payload_brief_for(kind: str | None) -> str:
    key = "__system__" if kind is None else kind
    return _KIND_PAYLOAD_BRIEF.get(key, "См. DATA_SCHEMA_RU.")


def payload_full_doc_for(kind: str | None) -> str:
    key = "__system__" if kind is None else kind
    return _KIND_PAYLOAD_FULL.get(key, DATA_SCHEMA_RU)


PROMPT_SEED_FLAGS: dict[str, tuple[str, str]] = {
    # flag_key -> (setting_key, task kind | "__system__")
    "week_group_prompt_v2": (f"{SETTING_PREFIX}week_group", "week_group"),
    "live_set_prompt_nodup_v1": (f"{SETTING_PREFIX}live_set", "live_set"),
    "week_plan_prompt_v1": (f"{SETTING_PREFIX}week_plan", "week_plan"),
    "live_set_prompt_rest_advice_v1": (f"{SETTING_PREFIX}live_set", "live_set"),
    "week_plan_prompt_rest_advice_v1": (f"{SETTING_PREFIX}week_plan", "week_plan"),
    "exercise_prompt_rest_advice_v1": (f"{SETTING_PREFIX}exercise", "exercise"),
    "system_prompt_rest_advice_v1": (SETTING_SYSTEM, "__system__"),
    "week_plan_prompt_machine_v1": (f"{SETTING_PREFIX}week_plan", "week_plan"),
    "live_set_prompt_machine_v1": (f"{SETTING_PREFIX}live_set", "live_set"),
    "week_prompt_year_history_v1": (f"{SETTING_PREFIX}week", "week"),
    "week_group_prompt_year_history_v1": (f"{SETTING_PREFIX}week_group", "week_group"),
    "week_plan_prompt_year_history_v1": (f"{SETTING_PREFIX}week_plan", "week_plan"),
}


async def ensure_prompt_seeds(session: AsyncSession) -> None:
    """One-shot overwrite AppSetting task prompts when product defaults change."""
    for flag, (setting_key, kind) in PROMPT_SEED_FLAGS.items():
        row = await session.get(AppSetting, flag)
        if row is not None:
            continue
        body = SYSTEM_PROMPT if kind == "__system__" else DEFAULT_TASKS[kind]
        existing = await session.get(AppSetting, setting_key)
        if existing is None:
            session.add(AppSetting(key=setting_key, value=body))
        else:
            existing.value = body
        session.add(AppSetting(key=flag, value="1"))
    await session.commit()
