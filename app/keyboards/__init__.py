from __future__ import annotations

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, KeyboardButton, ReplyKeyboardMarkup

from app.db.models import Difficulty, TemplateExercise, TrainingPhase, UserExerciseState
from app.services.progression import PHASE_LABELS


def main_menu(is_admin: bool = False) -> ReplyKeyboardMarkup:
    rows = [
        [KeyboardButton(text="Тренировка"), KeyboardButton(text="Сегодня")],
        [KeyboardButton(text="Программа"), KeyboardButton(text="Профиль")],
    ]
    if is_admin:
        rows.append([KeyboardButton(text="Админка")])
    return ReplyKeyboardMarkup(keyboard=rows, resize_keyboard=True)


def skip_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="Пропустить", callback_data="onb:skip_height")]]
    )


def phase_kb(current: TrainingPhase) -> InlineKeyboardMarkup:
    rows = []
    for phase in TrainingPhase:
        mark = "✓ " if phase == current else ""
        rows.append(
            [
                InlineKeyboardButton(
                    text=f"{mark}{PHASE_LABELS[phase]}",
                    callback_data=f"profile:phase:{phase.value}",
                )
            ]
        )
    return InlineKeyboardMarkup(inline_keyboard=rows)


def workout_exercise_kb(
    exercises: list[TemplateExercise],
    done_ids: set[int],
) -> InlineKeyboardMarkup:
    rows = []
    for ex in exercises:
        mark = "✅ " if ex.id in done_ids else ""
        rows.append(
            [
                InlineKeyboardButton(
                    text=f"{mark}{ex.name}",
                    callback_data=f"wo:ex:{ex.id}",
                )
            ]
        )
    rows.append([InlineKeyboardButton(text="Закончить тренировку", callback_data="wo:finish")])
    rows.append([InlineKeyboardButton(text="Отмена", callback_data="wo:cancel")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def weight_kb(
    exercise: TemplateExercise,
    state: UserExerciseState | None,
    draft_weight: float | None = None,
) -> InlineKeyboardMarkup:
    step = exercise.weight_step or 2.5
    base = draft_weight
    if base is None:
        if state and state.suggested_weight is not None:
            base = state.suggested_weight
        elif state and state.working_weight is not None:
            base = state.working_weight
        else:
            base = 20.0

    buttons = [
        [
            InlineKeyboardButton(text=f"−{step:g}", callback_data=f"wo:w:-:{step}"),
            InlineKeyboardButton(text=f"{base:g} кг", callback_data=f"wo:w:=:{base}"),
            InlineKeyboardButton(text=f"+{step:g}", callback_data=f"wo:w:+:{step}"),
        ]
    ]
    presets = []
    for delta in (-step * 2, 0, step * 2):
        val = max(0.0, base + delta)
        presets.append(
            InlineKeyboardButton(text=f"{val:g}", callback_data=f"wo:w:=:{val}")
        )
    buttons.append(presets)
    if state and state.working_weight is not None and state.working_weight != base:
        buttons.append(
            [
                InlineKeyboardButton(
                    text=f"Прошлый: {state.working_weight:g}",
                    callback_data=f"wo:w:=:{state.working_weight}",
                )
            ]
        )
    buttons.append([InlineKeyboardButton(text="Ввести вес", callback_data="wo:w:custom")])
    buttons.append([InlineKeyboardButton(text="« Назад", callback_data="wo:back")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def reps_kb(target_min: int, target_max: int, last_reps: int | None = None) -> InlineKeyboardMarkup:
    options = [6, 8, 10, 12, 15]
    for value in (target_min, target_max, last_reps):
        if value and value not in options:
            options.append(value)
    options = sorted(set(options))
    row = [
        InlineKeyboardButton(text=str(r), callback_data=f"wo:r:{r}") for r in options
    ]
    # split into rows of 4
    rows = [row[i : i + 4] for i in range(0, len(row), 4)]
    rows.append([InlineKeyboardButton(text="Отказ", callback_data="wo:r:0")])
    rows.append([InlineKeyboardButton(text="« Назад", callback_data="wo:back")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def sets_kb(target_sets: int, last_sets: int | None = None) -> InlineKeyboardMarkup:
    options = [2, 3, 4, 5]
    for value in (target_sets, last_sets):
        if value and value not in options:
            options.append(value)
    options = sorted(set(options))
    row = [InlineKeyboardButton(text=str(s), callback_data=f"wo:s:{s}") for s in options]
    return InlineKeyboardMarkup(
        inline_keyboard=[row, [InlineKeyboardButton(text="« Назад", callback_data="wo:back")]]
    )


def after_set_kb(target_sets: int, done_sets: int) -> InlineKeyboardMarkup:
    more = "Ещё подход"
    if target_sets and done_sets < target_sets:
        more = f"Ещё подход ({done_sets}/{target_sets})"
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=more, callback_data="wo:more")],
            [InlineKeyboardButton(text="Дропсет (другой вес в этом подходе)", callback_data="wo:drop")],
            [InlineKeyboardButton(text="Готово по упражнению", callback_data="wo:exdone")],
            [InlineKeyboardButton(text="Отменить последний кусок", callback_data="wo:undo")],
        ]
    )


def difficulty_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="Легко", callback_data=f"wo:d:{Difficulty.easy.value}"),
                InlineKeyboardButton(text="Норм", callback_data=f"wo:d:{Difficulty.normal.value}"),
            ],
            [
                InlineKeyboardButton(text="Тяжело", callback_data=f"wo:d:{Difficulty.hard.value}"),
                InlineKeyboardButton(text="Отказ", callback_data=f"wo:d:{Difficulty.failure.value}"),
            ],
            [InlineKeyboardButton(text="« Назад", callback_data="wo:back")],
        ]
    )


def admin_menu_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="Шаблоны дней", callback_data="adm:templates")],
            [InlineKeyboardButton(text="Архив упражнений", callback_data="adm:archive")],
            [InlineKeyboardButton(text="График недели", callback_data="adm:schedule")],
            [InlineKeyboardButton(text="Часы напоминаний", callback_data="adm:hours")],
            [InlineKeyboardButton(text="Пользователи", callback_data="adm:users")],
        ]
    )


def templates_list_kb(templates: list) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(text=t.name, callback_data=f"adm:tpl:{t.id}")]
        for t in templates
    ]
    rows.append([InlineKeyboardButton(text="+ Новый шаблон", callback_data="adm:tpl:new")])
    rows.append([InlineKeyboardButton(text="« Назад", callback_data="adm:home")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def template_detail_kb(template_id: int, exercises: list | None = None) -> InlineKeyboardMarkup:
    rows = []
    for ex in exercises or []:
        rows.append(
            [
                InlineKeyboardButton(
                    text=f"{ex.position + 1}. {ex.name}",
                    callback_data=f"adm:ex:view:{ex.id}",
                )
            ]
        )
    rows.append(
        [InlineKeyboardButton(text="+ Упражнение", callback_data=f"adm:ex:add:{template_id}")]
    )
    rows.append(
        [InlineKeyboardButton(text="Удалить шаблон", callback_data=f"adm:tpl:del:{template_id}")]
    )
    rows.append([InlineKeyboardButton(text="« К шаблонам", callback_data="adm:templates")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def exercise_edit_kb(exercise_id: int, template_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="Переименовать", callback_data=f"adm:ex:name:{exercise_id}")],
            [InlineKeyboardButton(text="Цели (подходы/повторы)", callback_data=f"adm:ex:tgt:{exercise_id}")],
            [InlineKeyboardButton(text="В другой шаблон", callback_data=f"adm:ex:move:{exercise_id}")],
            [InlineKeyboardButton(text="Удалить упражнение", callback_data=f"adm:ex:del:{exercise_id}")],
            [InlineKeyboardButton(text="« К шаблону", callback_data=f"adm:tpl:{template_id}")],
        ]
    )


def exercise_delete_confirm_kb(exercise_id: int, template_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="Да, удалить", callback_data=f"adm:ex:delok:{exercise_id}"),
                InlineKeyboardButton(text="Отмена", callback_data=f"adm:ex:view:{exercise_id}"),
            ],
            [InlineKeyboardButton(text="« К шаблону", callback_data=f"adm:tpl:{template_id}")],
        ]
    )


def exercise_move_kb(exercise_id: int, templates: list, current_template_id: int) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(text=t.name, callback_data=f"adm:ex:mvto:{exercise_id}:{t.id}")]
        for t in templates
        if t.id != current_template_id
    ]
    rows.append([InlineKeyboardButton(text="« Назад", callback_data=f"adm:ex:view:{exercise_id}")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def schedule_kb(assignments: dict[int, str]) -> InlineKeyboardMarkup:
    names = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"]
    rows = []
    for weekday, label in enumerate(names):
        current = assignments.get(weekday, "—")
        rows.append(
            [
                InlineKeyboardButton(
                    text=f"{label}: {current}",
                    callback_data=f"adm:sch:{weekday}",
                )
            ]
        )
    rows.append([InlineKeyboardButton(text="« Назад", callback_data="adm:home")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def schedule_pick_template_kb(weekday: int, templates: list) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(text=t.name, callback_data=f"adm:schset:{weekday}:{t.id}")]
        for t in templates
    ]
    rows.append(
        [InlineKeyboardButton(text="Очистить день", callback_data=f"adm:schclear:{weekday}")]
    )
    rows.append([InlineKeyboardButton(text="« Назад", callback_data="adm:schedule")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


ARCHIVE_PAGE = 8


def _page_nav(page: int, total: int, prefix: str) -> list[list[InlineKeyboardButton]]:
    pages = max(1, (total + ARCHIVE_PAGE - 1) // ARCHIVE_PAGE)
    if pages <= 1:
        return []
    row = []
    if page > 0:
        row.append(InlineKeyboardButton(text="‹", callback_data=f"{prefix}:{page - 1}"))
    row.append(InlineKeyboardButton(text=f"{page + 1}/{pages}", callback_data="adm:noop"))
    if page + 1 < pages:
        row.append(InlineKeyboardButton(text="›", callback_data=f"{prefix}:{page + 1}"))
    return [row]


def archive_pick_kb(template_id: int, items: list, page: int = 0) -> InlineKeyboardMarkup:
    start = page * ARCHIVE_PAGE
    chunk = items[start : start + ARCHIVE_PAGE]
    rows = [
        [
            InlineKeyboardButton(
                text=item.name,
                callback_data=f"adm:ex:from:{template_id}:{item.id}",
            )
        ]
        for item in chunk
    ]
    rows.extend(_page_nav(page, len(items), f"adm:ex:pick:{template_id}"))
    rows.append(
        [InlineKeyboardButton(text="+ Новое упражнение", callback_data=f"adm:ex:new:{template_id}")]
    )
    rows.append([InlineKeyboardButton(text="« К шаблону", callback_data=f"adm:tpl:{template_id}")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def archive_list_kb(items: list, page: int = 0) -> InlineKeyboardMarkup:
    start = page * ARCHIVE_PAGE
    chunk = items[start : start + ARCHIVE_PAGE]
    rows = [
        [InlineKeyboardButton(text=item.name, callback_data=f"adm:arch:v:{item.id}")]
        for item in chunk
    ]
    if not chunk:
        rows.append([InlineKeyboardButton(text="Пока пусто", callback_data="adm:noop")])
    rows.extend(_page_nav(page, len(items), "adm:arch:p"))
    rows.append([InlineKeyboardButton(text="« Назад", callback_data="adm:home")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def archive_item_kb(item_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="Удалить из архива", callback_data=f"adm:arch:del:{item_id}")],
            [InlineKeyboardButton(text="« К архиву", callback_data="adm:archive")],
        ]
    )
