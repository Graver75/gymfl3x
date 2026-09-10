"""Shared UI labels — light emoji language for menus and screens."""

from __future__ import annotations

from datetime import date, datetime
from html import escape as _html_escape


def esc(text: object) -> str:
    """Escape user/DB text for Telegram HTML parse mode."""
    return _html_escape(str(text), quote=False)


def b(text: object) -> str:
    """Bold HTML; escapes content first."""
    return f"<b>{esc(text)}</b>"

# Reply menu (exact texts for F.text filters)
BTN_WORKOUT = "🏋️ Тренировка"
BTN_TODAY = "📅 Сегодня"
BTN_HISTORY = "📜 История"
BTN_PROGRAM = "📋 Программа"
BTN_PROFILE = "👤 Профиль"
BTN_COACH = "🧠 ИИ-разбор"
BTN_ADMIN = "⚙️ Админка"

# Common actions
BTN_BACK = "⬅️ Назад"
BTN_CANCEL = "✖️ Отмена"
BTN_SKIP = "⏭️ Пропустить"
BTN_ENTER_WEIGHT = "⌨️ Ввести вес"
BTN_ENTER_REPS = "⌨️ Ввести число"
BTN_FINISH_WORKOUT = "🏁 Закончить тренировку"
BTN_MORE_SET = "➕ Ещё подход"
BTN_DROP_SET = "↘️ Дроп-сет"
BTN_EX_DONE = "✅ Готово по упражнению"
BTN_UNDO_SET = "↩️ Отменить последний подход"
BTN_RESET_WORKOUT = "🔄 Сбросить тренировку"
BTN_RESET_WORKOUT_OK = "🗑️ Да, сбросить подходы"
BTN_PROFILE_PROGRESS = "🏅 Мой прогресс"
BTN_PROFILE_RESET = "🧹 Обнулить мои данные"
BTN_PROFILE_RESET_OK = "🗑️ Да, обнулить всё"

BTN_OPEN_BOT = "🚀 Открыть бота"
BTN_CHECKIN_SKIP = "⏭️ Пропустить чекин"
BTN_CHECKIN_DONE = "✅ Готово"

# Log levels
LOG_LEVEL_LABELS = {
    "minimal": "Минимум",
    "standard": "Стандарт",
    "detailed": "Подробно",
}
LOG_LEVEL_HINTS = {
    "minimal": "вес, повторы, сложность, заметки",
    "standard": "+ чекин в конце: энергия / сон / боль",
    "detailed": "+ RPE на каждый подход и чекин",
}

# Difficulty
BTN_EASY = "😌 Легко"
BTN_NORMAL = "😐 Норм"
BTN_HARD = "😤 Тяжело"
BTN_FAILURE = "💀 Отказ"
BTN_REP_FAIL = "🚫 Отказ"
BTN_NOTE = "📝 Заметка"

# Workout start
BTN_MODE_TODAY = "📅 По графику сегодня"
BTN_MODE_FREE = "✨ Свободная тренировка"
BTN_FROM_ARCHIVE = "📦 Из архива упражнений"

# History
BTN_HIST_SESSIONS = "📅 Последние тренировки"
BTN_HIST_EXERCISES = "💪 По упражнениям"
BTN_HIST_WEEK = "📊 Сводка за неделю"
BTN_HIST_EDIT_LAST = "✏️ Править последнюю"
BTN_HIST_OTHERS = "👥 Чужая история"
BTN_HIST_ATHLETES = "⬅️ К атлетам"
BTN_HIST_MY = "📜 Моя история"
BTN_HIST_ADMIN = "⚙️ В админку"
BTN_HIST_EDIT_SETS = "✏️ Править подходы"
BTN_HIST_DELETE_SESSION = "🗑️ Удалить тренировку"
BTN_HIST_DELETE_CONFIRM = "🗑️ Да, удалить"

# Admin
BTN_ADM_TEMPLATES = "📋 Шаблоны дней"
BTN_ADM_CURRENT = "💪 Текущие упражнения"
BTN_ADM_ARCHIVE = "📦 Архив упражнений"
BTN_ADM_SCHEDULE = "📅 График недели"
BTN_ADM_HOURS = "⏰ Часы напоминаний"
BTN_ADM_ATHLETES_HIST = "📜 История атлетов"
BTN_ADM_MISSING = "👀 Кто не залогировал"
BTN_ADM_USERS = "👥 Пользователи"
BTN_ADM_CHATS = "💬 Чаты бота"
BTN_ADM_LEVELS = "🏅 Уровни силы"
BTN_ADM_SNAPSHOT = "🧠 Снимок для NN"
BTN_ADM_NN_LOAD = "📊 Нагрузка NN"

# Coach / NN
BTN_COACH_WEEK = "📊 Разбор недели"
BTN_COACH_MONTH = "📅 Разбор месяца"
BTN_COACH_EXERCISE = "💪 Совет по упражнению"
BTN_COACH_SET = "🧠 Совет ИИ"
BTN_COACH_PROMPT = "📜 Промпт и данные"
BTN_COACH_CLEAR = "🧹 Очистить диалог"
BTN_COACH_CLEAR_OK = "🗑️ Да, очистить диалог"
BTN_COACH_REFRESH = "🔄 Обновить статус"
ICO_NN = "🧠"

# Screen markers
ICO_EXERCISE = "🏋️"
ICO_TARGET = "🎯"
ICO_PR = "🏆"
ICO_REST = "⏱"
ICO_SET = "📦"
ICO_LAST = "📌"
ICO_NOTE = "📝"
ICO_DONE = "✅"
ICO_NEXT = "➡️"
ICO_MAP = "🗺️"
ICO_HISTORY = "📜"
ICO_ADMIN = "⚙️"
ICO_WAVE = "👋"
ICO_FIRE = "🔥"
ICO_RECAP = "📝"


def format_user_date(value: date | datetime | str) -> str:
    """User-facing dates as dd.mm.yyyy."""
    if isinstance(value, datetime):
        value = value.date()
    elif isinstance(value, str):
        raw = value.strip()
        if "T" in raw:
            value = datetime.fromisoformat(raw).date()
        else:
            value = date.fromisoformat(raw)
    return value.strftime("%d.%m.%Y")


def label_target(text: str) -> str:
    return f"{ICO_TARGET} Цель: {text}"


def label_pr(text: str) -> str:
    return f"{ICO_PR} {text}" if not text.startswith("PR") else f"{ICO_PR} {text}"


def label_rest(line: str) -> str:
    """Turn '\\nОтдых: m:ss' into emoji form; pass-through empty."""
    if not line:
        return ""
    raw = line.strip()
    if raw.startswith("Отдых:"):
        return f"\n{ICO_REST} {raw}"
    if raw.startswith("\nОтдых:"):
        return f"\n{ICO_REST} {raw.lstrip()}"
    return line.replace("Отдых:", f"{ICO_REST} Отдых:", 1)


def label_set(n: int, drop_index: int = 0) -> str:
    if drop_index > 0:
        return f"{ICO_SET} Подход {n} · дроп {drop_index}"
    return f"{ICO_SET} Подход {n}"


def label_exercise(name: str) -> str:
    return f"{ICO_EXERCISE} {name}"


def more_set_label(target_sets: int, done_sets: int) -> str:
    if target_sets and done_sets < target_sets:
        return f"{BTN_MORE_SET} ({done_sets}/{target_sets})"
    return BTN_MORE_SET
