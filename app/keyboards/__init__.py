from __future__ import annotations

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, KeyboardButton, ReplyKeyboardMarkup

from app.db.models import Difficulty, LogLevel, TemplateExercise, TrainingPhase, UserExerciseState
from app.services.progression import PHASE_LABELS
from app import ui_copy as ui


def main_menu(*, show_admin: bool = False) -> ReplyKeyboardMarkup:
    rows = [
        [KeyboardButton(text=ui.BTN_WORKOUT), KeyboardButton(text=ui.BTN_TODAY)],
        [KeyboardButton(text=ui.BTN_HISTORY), KeyboardButton(text=ui.BTN_PROGRAM)],
        [KeyboardButton(text=ui.BTN_PROFILE), KeyboardButton(text=ui.BTN_COACH)],
    ]
    if show_admin:
        rows.append([KeyboardButton(text=ui.BTN_ADMIN)])
    return ReplyKeyboardMarkup(keyboard=rows, resize_keyboard=True)


def skip_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text=ui.BTN_SKIP, callback_data="onb:skip_height")]]
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


def profile_kb(current_phase: TrainingPhase, current_log: LogLevel) -> InlineKeyboardMarkup:
    rows = []
    for phase in TrainingPhase:
        mark = "✓ " if phase == current_phase else ""
        rows.append(
            [
                InlineKeyboardButton(
                    text=f"{mark}{PHASE_LABELS[phase]}",
                    callback_data=f"profile:phase:{phase.value}",
                )
            ]
        )
    rows.append(
        [InlineKeyboardButton(text="—— Детализация лога ——", callback_data="adm:noop")]
    )
    level_row = []
    for level in LogLevel:
        mark = "✓ " if level == current_log else ""
        label = ui.LOG_LEVEL_LABELS.get(level.value, level.value)
        level_row.append(
            InlineKeyboardButton(
                text=f"{mark}{label}",
                callback_data=f"profile:log:{level.value}",
            )
        )
    rows.append(level_row)
    rows.append(
        [InlineKeyboardButton(text=ui.BTN_COACH, callback_data="coach:menu")]
    )
    rows.append(
        [InlineKeyboardButton(text=ui.BTN_PROFILE_RESET, callback_data="profile:reset")]
    )
    return InlineKeyboardMarkup(inline_keyboard=rows)


def profile_reset_confirm_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=ui.BTN_PROFILE_RESET_OK,
                    callback_data="profile:resetok",
                ),
                InlineKeyboardButton(text=ui.BTN_CANCEL, callback_data="profile:home"),
            ]
        ]
    )


def scale_1_5_kb(prefix: str, *, skip_label: str | None = None) -> InlineKeyboardMarkup:
    row = [
        InlineKeyboardButton(text=str(n), callback_data=f"{prefix}:{n}") for n in range(1, 6)
    ]
    rows = [row]
    if skip_label:
        rows.append([InlineKeyboardButton(text=skip_label, callback_data=f"{prefix}:skip")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def rpe_kb() -> InlineKeyboardMarkup:
    row1 = [InlineKeyboardButton(text=str(n), callback_data=f"wo:rpe:{n}") for n in range(1, 6)]
    row2 = [InlineKeyboardButton(text=str(n), callback_data=f"wo:rpe:{n}") for n in range(6, 11)]
    return InlineKeyboardMarkup(
        inline_keyboard=[
            row1,
            row2,
            [
                InlineKeyboardButton(text=ui.BTN_SKIP, callback_data="wo:rpe:skip"),
                InlineKeyboardButton(text=ui.BTN_BACK, callback_data="wo:back"),
            ],
        ]
    )


def workout_mode_kb(*, has_today: bool) -> InlineKeyboardMarkup:
    rows = []
    if has_today:
        rows.append(
            [InlineKeyboardButton(text=ui.BTN_MODE_TODAY, callback_data="wo:mode:today")]
        )
    rows.append([InlineKeyboardButton(text=ui.BTN_MODE_FREE, callback_data="wo:mode:free")])
    rows.append([InlineKeyboardButton(text=ui.BTN_CANCEL, callback_data="wo:cancel")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def workout_templates_kb(templates: list) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(text=f"📋 {t.name}", callback_data=f"wo:tpl:{t.id}")]
        for t in templates
    ]
    rows.append([InlineKeyboardButton(text=ui.BTN_FROM_ARCHIVE, callback_data="wo:arch")])
    rows.append([InlineKeyboardButton(text=ui.BTN_BACK, callback_data="wo:mode:back")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def workout_archive_kb(items: list, *, page: int = 0) -> InlineKeyboardMarkup:
    page_size = 8
    start = page * page_size
    chunk = items[start : start + page_size]
    rows = [
        [InlineKeyboardButton(text=item.name, callback_data=f"wo:archpick:{item.id}")]
        for item in chunk
    ]
    nav = []
    pages = max(1, (len(items) + page_size - 1) // page_size) if items else 1
    if page > 0:
        nav.append(InlineKeyboardButton(text="‹", callback_data=f"wo:archp:{page - 1}"))
    if pages > 1:
        nav.append(InlineKeyboardButton(text=f"{page + 1}/{pages}", callback_data="adm:noop"))
    if page + 1 < pages:
        nav.append(InlineKeyboardButton(text="›", callback_data=f"wo:archp:{page + 1}"))
    if nav:
        rows.append(nav)
    if not chunk:
        rows.append([InlineKeyboardButton(text="📭 Архив пуст", callback_data="adm:noop")])
    rows.append([InlineKeyboardButton(text=ui.BTN_FINISH_WORKOUT, callback_data="wo:finish")])
    rows.append([InlineKeyboardButton(text=ui.BTN_BACK, callback_data="wo:mode:free")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def workout_exercise_kb(
    exercises: list[TemplateExercise],
    done_ids: set[int],
    *,
    weight_hints: dict[int, float] | None = None,
    next_id: int | None = None,
    can_reset: bool = False,
) -> InlineKeyboardMarkup:
    rows = []
    for ex in exercises:
        if ex.id in done_ids:
            mark = f"{ui.ICO_DONE} "
        elif next_id is not None and ex.id == next_id:
            mark = f"{ui.ICO_NEXT} "
        else:
            mark = ""
        target = f"{ex.target_sets}×{ex.target_reps_min}-{ex.target_reps_max}"
        hint = ""
        if weight_hints and ex.id in weight_hints and ex.id not in done_ids:
            hint = f" · {weight_hints[ex.id]:g}кг"
        rows.append(
            [
                InlineKeyboardButton(
                    text=f"{mark}{ex.name} ({target}){hint}",
                    callback_data=f"wo:ex:{ex.id}",
                )
            ]
        )
    rows.append([InlineKeyboardButton(text=ui.BTN_FINISH_WORKOUT, callback_data="wo:finish")])
    if can_reset:
        rows.append(
            [InlineKeyboardButton(text=ui.BTN_RESET_WORKOUT, callback_data="wo:reset")]
        )
    rows.append([InlineKeyboardButton(text=ui.BTN_CANCEL, callback_data="wo:cancel")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def workout_reset_confirm_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=ui.BTN_RESET_WORKOUT_OK, callback_data="wo:resetok"
                )
            ],
            [InlineKeyboardButton(text=ui.BTN_BACK, callback_data="wo:map")],
        ]
    )


def weight_kb(
    exercise: TemplateExercise,
    state: UserExerciseState | None,
    draft_weight: float | None = None,
    *,
    weight_options: list[float] | None = None,
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
    presets: list[InlineKeyboardButton] = []
    values: list[float] = []
    if weight_options:
        values.extend(weight_options)
    else:
        for delta in (-step * 2, 0, step * 2):
            values.append(max(0.0, base + delta))
    seen: set[float] = set()
    for val in values:
        key = round(float(val), 2)
        if key in seen:
            continue
        seen.add(key)
        mark = "✓ " if abs(float(val) - float(base)) < 0.01 else ""
        presets.append(
            InlineKeyboardButton(text=f"{mark}{val:g}", callback_data=f"wo:w:=:{val}")
        )
        if len(presets) >= 6:
            break
    for i in range(0, len(presets), 3):
        buttons.append(presets[i : i + 3])
    buttons.append([InlineKeyboardButton(text=ui.BTN_ENTER_WEIGHT, callback_data="wo:w:custom")])
    buttons.append([InlineKeyboardButton(text=ui.BTN_COACH_SET, callback_data="wo:coach")])
    buttons.append([InlineKeyboardButton(text=ui.BTN_BACK, callback_data="wo:back")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def reps_kb(
    target_min: int,
    target_max: int,
    last_reps: int | None = None,
    *,
    reps_options: list[int] | None = None,
) -> InlineKeyboardMarkup:
    options = list(reps_options or [8, 10, 12, 15])
    for value in (target_min, target_max, last_reps):
        if value and value not in options:
            options.append(value)
    options = sorted(set(options))
    row = [
        InlineKeyboardButton(
            text=("✓ " if last_reps and r == last_reps else "") + str(r),
            callback_data=f"wo:r:{r}",
        )
        for r in options
    ]
    rows = [row[i : i + 4] for i in range(0, len(row), 4)]
    rows.append([InlineKeyboardButton(text=ui.BTN_REP_FAIL, callback_data="wo:r:0")])
    rows.append([InlineKeyboardButton(text=ui.BTN_ENTER_REPS, callback_data="wo:r:custom")])
    rows.append([InlineKeyboardButton(text=ui.BTN_COACH_SET, callback_data="wo:coach")])
    rows.append([InlineKeyboardButton(text=ui.BTN_BACK, callback_data="wo:back")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def sets_kb(target_sets: int, last_sets: int | None = None) -> InlineKeyboardMarkup:
    options = [2, 3, 4, 5]
    for value in (target_sets, last_sets):
        if value and value not in options:
            options.append(value)
    options = sorted(set(options))
    row = [InlineKeyboardButton(text=str(s), callback_data=f"wo:s:{s}") for s in options]
    return InlineKeyboardMarkup(
        inline_keyboard=[row, [InlineKeyboardButton(text=ui.BTN_BACK, callback_data="wo:back")]]
    )


def after_set_kb(target_sets: int, done_sets: int) -> InlineKeyboardMarkup:
    more = ui.more_set_label(target_sets, done_sets)
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=more, callback_data="wo:more")],
            [InlineKeyboardButton(text=ui.BTN_EX_DONE, callback_data="wo:exdone")],
            [InlineKeyboardButton(text=ui.BTN_UNDO_SET, callback_data="wo:undo")],
            [InlineKeyboardButton(text=ui.BTN_COACH_SET, callback_data="wo:coach")],
        ]
    )


def difficulty_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text=ui.BTN_EASY, callback_data=f"wo:d:{Difficulty.easy.value}"),
                InlineKeyboardButton(text=ui.BTN_NORMAL, callback_data=f"wo:d:{Difficulty.normal.value}"),
            ],
            [
                InlineKeyboardButton(text=ui.BTN_HARD, callback_data=f"wo:d:{Difficulty.hard.value}"),
                InlineKeyboardButton(text=ui.BTN_FAILURE, callback_data=f"wo:d:{Difficulty.failure.value}"),
            ],
            [InlineKeyboardButton(text=ui.BTN_NOTE, callback_data="wo:note")],
            [InlineKeyboardButton(text=ui.BTN_BACK, callback_data="wo:back")],
        ]
    )


def admin_menu_kb(*, full: bool = True) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(text=ui.BTN_ADM_TEMPLATES, callback_data="adm:templates")],
        [InlineKeyboardButton(text=ui.BTN_ADM_CURRENT, callback_data="adm:current")],
        [InlineKeyboardButton(text=ui.BTN_ADM_ARCHIVE, callback_data="adm:archive")],
        [InlineKeyboardButton(text=ui.BTN_ADM_SCHEDULE, callback_data="adm:schedule")],
    ]
    if full:
        rows.extend(
            [
                [InlineKeyboardButton(text=ui.BTN_ADM_HOURS, callback_data="adm:hours")],
                [
                    InlineKeyboardButton(
                        text=ui.BTN_ADM_ATHLETES_HIST, callback_data="hist:athletes:adm"
                    )
                ],
                [InlineKeyboardButton(text=ui.BTN_ADM_MISSING, callback_data="adm:missing")],
                [InlineKeyboardButton(text=ui.BTN_ADM_USERS, callback_data="adm:users")],
                [InlineKeyboardButton(text=ui.BTN_ADM_CHATS, callback_data="adm:chats")],
                [InlineKeyboardButton(text=ui.BTN_ADM_SNAPSHOT, callback_data="adm:snapshot")],
                [InlineKeyboardButton(text=ui.BTN_ADM_NN_LOAD, callback_data="adm:nnload")],
            ]
        )
    return InlineKeyboardMarkup(inline_keyboard=rows)


def admin_chats_kb(destinations: list | None = None, *, page: int = 0) -> InlineKeyboardMarkup:
    """destinations: list of ChatProbe (or objects with chat_id/button_label/can_write)."""
    items = list(destinations or [])
    page_size = 8
    start = page * page_size
    chunk = items[start : start + page_size]
    rows: list[list[InlineKeyboardButton]] = []
    for d in chunk:
        rows.append(
            [
                InlineKeyboardButton(
                    text=d.button_label,
                    callback_data=f"adm:chat:to:{d.chat_id}",
                )
            ]
        )
    pages = max(1, (len(items) + page_size - 1) // page_size) if items else 1
    nav: list[InlineKeyboardButton] = []
    if page > 0:
        nav.append(InlineKeyboardButton(text="‹", callback_data=f"adm:chats:p:{page - 1}"))
    if pages > 1:
        nav.append(InlineKeyboardButton(text=f"{page + 1}/{pages}", callback_data="adm:noop"))
    if page + 1 < pages:
        nav.append(InlineKeyboardButton(text="›", callback_data=f"adm:chats:p:{page + 1}"))
    if nav:
        rows.append(nav)
    rows.append([InlineKeyboardButton(text="🔄 Обновить статусы", callback_data="adm:chats")])
    rows.append([InlineKeyboardButton(text=ui.BTN_BACK, callback_data="adm:home")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def admin_compose_cancel_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=ui.BTN_CANCEL, callback_data="adm:chat:cancel")],
        ]
    )


def admin_nn_load_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="🔄 Обновить", callback_data="adm:nnload")],
            [InlineKeyboardButton(text=ui.BTN_BACK, callback_data="adm:home")],
        ]
    )


def current_exercises_kb(items: list[tuple], *, page: int = 0) -> InlineKeyboardMarkup:
    """items: list of (exercise_id, label)."""
    page_size = 10
    start = page * page_size
    chunk = items[start : start + page_size]
    rows = [
        [InlineKeyboardButton(text=label[:64], callback_data=f"adm:cur:ex:{ex_id}")]
        for ex_id, label in chunk
    ]
    nav = []
    pages = max(1, (len(items) + page_size - 1) // page_size) if items else 1
    if page > 0:
        nav.append(InlineKeyboardButton(text="‹", callback_data=f"adm:cur:p:{page - 1}"))
    if pages > 1:
        nav.append(InlineKeyboardButton(text=f"{page + 1}/{pages}", callback_data="adm:noop"))
    if page + 1 < pages:
        nav.append(InlineKeyboardButton(text="›", callback_data=f"adm:cur:p:{page + 1}"))
    if nav:
        rows.append(nav)
    if not chunk:
        rows.append([InlineKeyboardButton(text="📭 Пока пусто", callback_data="adm:noop")])
    rows.append([InlineKeyboardButton(text=ui.BTN_BACK, callback_data="adm:home")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def admin_users_kb(users: list) -> InlineKeyboardMarkup:
    rows = []
    for u in users:
        if u.is_admin:
            mark = "⭐ admin"
            rows.append(
                [
                    InlineKeyboardButton(
                        text=f"{mark} · {u.short_code} {u.display_name}",
                        callback_data="adm:noop",
                    )
                ]
            )
            continue
        flag = "✓" if u.is_program_admin else "✗"
        rows.append(
            [
                InlineKeyboardButton(
                    text=f"[{flag} прог] {u.short_code} · {u.display_name}",
                    callback_data=f"adm:u:prog:{u.id}",
                )
            ]
        )
    if not rows:
        rows.append([InlineKeyboardButton(text="📭 Пока никого", callback_data="adm:noop")])
    rows.append([InlineKeyboardButton(text=ui.BTN_BACK, callback_data="adm:home")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def templates_list_kb(templates: list) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(text=f"📋 {t.name}", callback_data=f"adm:tpl:{t.id}")]
        for t in templates
    ]
    rows.append([InlineKeyboardButton(text="➕ Новый шаблон", callback_data="adm:tpl:new")])
    rows.append([InlineKeyboardButton(text=ui.BTN_BACK, callback_data="adm:home")])
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
        [InlineKeyboardButton(text="➕ Упражнение", callback_data=f"adm:ex:add:{template_id}")]
    )
    rows.append(
        [InlineKeyboardButton(text="🗑️ Удалить шаблон", callback_data=f"adm:tpl:del:{template_id}")]
    )
    rows.append([InlineKeyboardButton(text="⬅️ К шаблонам", callback_data="adm:templates")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def exercise_edit_kb(
    exercise_id: int,
    template_id: int,
    *,
    back: str | None = None,
) -> InlineKeyboardMarkup:
    back_cb = back or f"adm:tpl:{template_id}"
    back_label = "⬅️ К текущим" if back_cb == "adm:current" else "⬅️ К шаблону"
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="⬆️ Выше", callback_data=f"adm:ex:up:{exercise_id}"),
                InlineKeyboardButton(text="⬇️ Ниже", callback_data=f"adm:ex:dn:{exercise_id}"),
            ],
            [InlineKeyboardButton(text="✏️ Переименовать", callback_data=f"adm:ex:name:{exercise_id}")],
            [InlineKeyboardButton(text="🎯 Цели (подходы/повторы)", callback_data=f"adm:ex:tgt:{exercise_id}")],
            [InlineKeyboardButton(text="↗️ В другой шаблон", callback_data=f"adm:ex:move:{exercise_id}")],
            [InlineKeyboardButton(text="🗑️ Удалить упражнение", callback_data=f"adm:ex:del:{exercise_id}")],
            [InlineKeyboardButton(text=back_label, callback_data=back_cb)],
        ]
    )


def exercise_delete_confirm_kb(
    exercise_id: int,
    template_id: int,
    *,
    back: str | None = None,
) -> InlineKeyboardMarkup:
    back_cb = back or f"adm:tpl:{template_id}"
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="🗑️ Да, удалить", callback_data=f"adm:ex:delok:{exercise_id}"),
                InlineKeyboardButton(text=ui.BTN_CANCEL, callback_data=f"adm:ex:view:{exercise_id}"),
            ],
            [InlineKeyboardButton(text=ui.BTN_BACK, callback_data=back_cb)],
        ]
    )


def exercise_move_kb(exercise_id: int, templates: list, current_template_id: int) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(text=t.name, callback_data=f"adm:ex:mvto:{exercise_id}:{t.id}")]
        for t in templates
        if t.id != current_template_id
    ]
    rows.append([InlineKeyboardButton(text=ui.BTN_BACK, callback_data=f"adm:ex:view:{exercise_id}")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def schedule_kb(assignments: dict[int, str]) -> InlineKeyboardMarkup:
    names = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"]
    rows = []
    for weekday, label in enumerate(names):
        current = assignments.get(weekday, "—")
        rows.append(
            [
                InlineKeyboardButton(
                    text=f"📅 {label}: {current}",
                    callback_data=f"adm:sch:{weekday}",
                )
            ]
        )
    rows.append([InlineKeyboardButton(text=ui.BTN_BACK, callback_data="adm:home")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def schedule_pick_template_kb(weekday: int, templates: list) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(text=t.name, callback_data=f"adm:schset:{weekday}:{t.id}")]
        for t in templates
    ]
    rows.append(
        [InlineKeyboardButton(text="🧹 Очистить день", callback_data=f"adm:schclear:{weekday}")]
    )
    rows.append([InlineKeyboardButton(text=ui.BTN_BACK, callback_data="adm:schedule")])
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


def archive_pick_kb(
    template_id: int,
    items: list,
    page: int = 0,
    *,
    in_template_keys: set[str] | None = None,
) -> InlineKeyboardMarkup:
    """Full catalog; exercises already in this template are shown as ✓ (noop)."""
    in_tpl = in_template_keys or set()
    start = page * ARCHIVE_PAGE
    chunk = items[start : start + ARCHIVE_PAGE]
    rows = []
    for item in chunk:
        if item.name_key in in_tpl:
            rows.append(
                [
                    InlineKeyboardButton(
                        text=f"✓ {item.name}",
                        callback_data="adm:noop",
                    )
                ]
            )
        else:
            rows.append(
                [
                    InlineKeyboardButton(
                        text=item.name,
                        callback_data=f"adm:ex:from:{template_id}:{item.id}",
                    )
                ]
            )
    if not chunk:
        rows.append([InlineKeyboardButton(text="📭 Пока пусто", callback_data="adm:noop")])
    rows.extend(_page_nav(page, len(items), f"adm:ex:pick:{template_id}"))
    rows.append(
        [InlineKeyboardButton(text="➕ Новое упражнение", callback_data=f"adm:ex:new:{template_id}")]
    )
    rows.append([InlineKeyboardButton(text="⬅️ К шаблону", callback_data=f"adm:tpl:{template_id}")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def archive_list_kb(items: list, page: int = 0) -> InlineKeyboardMarkup:
    start = page * ARCHIVE_PAGE
    chunk = items[start : start + ARCHIVE_PAGE]
    rows = [
        [InlineKeyboardButton(text=item.name, callback_data=f"adm:arch:v:{item.id}")]
        for item in chunk
    ]
    if not chunk:
        rows.append([InlineKeyboardButton(text="📭 Пока пусто", callback_data="adm:noop")])
    rows.extend(_page_nav(page, len(items), "adm:arch:p"))
    rows.append([InlineKeyboardButton(text=ui.BTN_BACK, callback_data="adm:home")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def archive_item_kb(item_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="🗑️ Удалить из архива", callback_data=f"adm:arch:del:{item_id}")],
            [InlineKeyboardButton(text="⬅️ К архиву", callback_data="adm:archive")],
        ]
    )


def history_home_kb(*, is_admin: bool = False, viewing_other: bool = False) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(text=ui.BTN_HIST_SESSIONS, callback_data="hist:sessions")],
        [InlineKeyboardButton(text=ui.BTN_HIST_EXERCISES, callback_data="hist:exercises")],
    ]
    if viewing_other:
        rows.append([InlineKeyboardButton(text=ui.BTN_HIST_WEEK, callback_data="hist:week")])
        rows.append([InlineKeyboardButton(text=ui.BTN_HIST_ATHLETES, callback_data="hist:athletes")])
    else:
        rows.append([InlineKeyboardButton(text=ui.BTN_HIST_EDIT_LAST, callback_data="hist:editlast")])
        if is_admin:
            rows.append(
                [InlineKeyboardButton(text=ui.BTN_HIST_OTHERS, callback_data="hist:athletes")]
            )
    return InlineKeyboardMarkup(inline_keyboard=rows)


def history_athletes_kb(
    users: list,
    *,
    page: int = 0,
    back: str = "hist:home",
) -> InlineKeyboardMarkup:
    page_size = 8
    start = page * page_size
    chunk = users[start : start + page_size]
    rows = []
    for u in chunk:
        mark = "⭐ " if getattr(u, "is_admin", False) else "👤 "
        rows.append(
            [
                InlineKeyboardButton(
                    text=f"{mark}{u.short_code} · {u.display_name}",
                    callback_data=f"hist:au:{u.id}",
                )
            ]
        )
    nav = []
    pages = max(1, (len(users) + page_size - 1) // page_size) if users else 1
    if page > 0:
        nav.append(InlineKeyboardButton(text="‹", callback_data=f"hist:ap:{page - 1}"))
    if pages > 1:
        nav.append(InlineKeyboardButton(text=f"{page + 1}/{pages}", callback_data="adm:noop"))
    if page + 1 < pages:
        nav.append(InlineKeyboardButton(text="›", callback_data=f"hist:ap:{page + 1}"))
    if nav:
        rows.append(nav)
    if not chunk:
        rows.append([InlineKeyboardButton(text="📭 Пока никого", callback_data="adm:noop")])
    back_label = ui.BTN_HIST_ADMIN if back == "adm:home" else ui.BTN_HIST_MY
    rows.append([InlineKeyboardButton(text=back_label, callback_data=back)])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def history_sessions_kb(sessions: list, *, back: str = "hist:home") -> InlineKeyboardMarkup:
    rows = []
    for ws in sessions:
        title = ws.template.name if ws.template else "Тренировка"
        rows.append(
            [
                InlineKeyboardButton(
                    text=f"📅 {ui.format_user_date(ws.session_date)} · {title}",
                    callback_data=f"hist:s:{ws.id}",
                )
            ]
        )
    if not rows:
        rows.append([InlineKeyboardButton(text="📭 Пока пусто", callback_data="adm:noop")])
    rows.append([InlineKeyboardButton(text=ui.BTN_BACK, callback_data=back)])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def history_exercises_kb(names: list[str], page: int = 0, *, back: str = "hist:home") -> InlineKeyboardMarkup:
    page_size = 8
    start = page * page_size
    chunk = names[start : start + page_size]
    rows = [
        [InlineKeyboardButton(text=f"💪 {name}", callback_data=f"hist:e:{start + offset}")]
        for offset, name in enumerate(chunk)
    ]
    nav = []
    pages = max(1, (len(names) + page_size - 1) // page_size) if names else 1
    if page > 0:
        nav.append(InlineKeyboardButton(text="‹", callback_data=f"hist:ep:{page - 1}"))
    if pages > 1:
        nav.append(InlineKeyboardButton(text=f"{page + 1}/{pages}", callback_data="adm:noop"))
    if page + 1 < pages:
        nav.append(InlineKeyboardButton(text="›", callback_data=f"hist:ep:{page + 1}"))
    if nav:
        rows.append(nav)
    if not chunk:
        rows.append([InlineKeyboardButton(text="📭 Пока пусто", callback_data="adm:noop")])
    rows.append([InlineKeyboardButton(text=ui.BTN_BACK, callback_data=back)])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def history_session_detail_kb(
    session_id: int,
    *,
    can_edit: bool,
    back: str = "hist:sessions",
) -> InlineKeyboardMarkup:
    rows = []
    if can_edit:
        rows.append(
            [InlineKeyboardButton(text=ui.BTN_HIST_EDIT_SETS, callback_data=f"hist:edit:{session_id}")]
        )
        rows.append(
            [
                InlineKeyboardButton(
                    text=ui.BTN_HIST_DELETE_SESSION,
                    callback_data=f"hist:sdel:{session_id}",
                )
            ]
        )
    rows.append([InlineKeyboardButton(text=ui.BTN_BACK, callback_data=back)])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def history_delete_session_kb(session_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=ui.BTN_HIST_DELETE_CONFIRM,
                    callback_data=f"hist:sdelok:{session_id}",
                ),
                InlineKeyboardButton(text=ui.BTN_CANCEL, callback_data=f"hist:s:{session_id}"),
            ]
        ]
    )


def history_edit_sets_kb(sets: list, session_id: int) -> InlineKeyboardMarkup:
    rows = []
    for s in sets:
        drop = f" d{s.drop_index}" if (s.drop_index or 0) else ""
        label = f"#{s.set_number}{drop}: {s.reps}×{s.weight:g} · {s.exercise_name}"
        rows.append(
            [InlineKeyboardButton(text=label[:64], callback_data=f"hist:es:{s.id}")]
        )
    if not rows:
        rows.append([InlineKeyboardButton(text="📭 Пусто", callback_data="adm:noop")])
    rows.append([InlineKeyboardButton(text="⬅️ К тренировке", callback_data=f"hist:s:{session_id}")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def history_edit_set_kb(set_id: int, session_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="⚖️ Изменить вес", callback_data=f"hist:ew:{set_id}")],
            [InlineKeyboardButton(text="🔢 Изменить повторы", callback_data=f"hist:er:{set_id}")],
            [InlineKeyboardButton(text="🗑️ Удалить подход", callback_data=f"hist:edel:{set_id}")],
            [InlineKeyboardButton(text="⬅️ К подходам", callback_data=f"hist:edit:{session_id}")],
        ]
    )


def history_back_kb(to: str = "hist:home") -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text=ui.BTN_BACK, callback_data=to)]]
    )


def coach_menu_kb(*, online: bool, turns: int = 0) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    if online:
        rows.extend(
            [
                [InlineKeyboardButton(text=ui.BTN_COACH_WEEK, callback_data="coach:week")],
                [InlineKeyboardButton(text=ui.BTN_COACH_MONTH, callback_data="coach:month")],
                [
                    InlineKeyboardButton(
                        text=ui.BTN_COACH_EXERCISE, callback_data="coach:exlist"
                    )
                ],
            ]
        )
    rows.append(
        [InlineKeyboardButton(text=ui.BTN_COACH_PROMPT, callback_data="coach:prompt")]
    )
    clear_label = ui.BTN_COACH_CLEAR
    if turns:
        clear_label = f"{ui.BTN_COACH_CLEAR} ({turns})"
    rows.append(
        [InlineKeyboardButton(text=clear_label, callback_data="coach:clear")]
    )
    rows.append(
        [InlineKeyboardButton(text=ui.BTN_COACH_REFRESH, callback_data="coach:refresh")]
    )
    rows.append([InlineKeyboardButton(text=ui.BTN_BACK, callback_data="profile:home")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def coach_clear_confirm_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=ui.BTN_COACH_CLEAR_OK, callback_data="coach:clearok"
                )
            ],
            [InlineKeyboardButton(text=ui.BTN_BACK, callback_data="coach:menu")],
        ]
    )


def coach_prompt_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=ui.BTN_BACK, callback_data="coach:menu")],
        ]
    )


def coach_exercises_kb(
    items: list[tuple[int, str]],
    *,
    page: int = 0,
    per_page: int = 8,
) -> InlineKeyboardMarkup:
    start = page * per_page
    chunk = items[start : start + per_page]
    rows: list[list[InlineKeyboardButton]] = [
        [InlineKeyboardButton(text=name[:40], callback_data=f"coach:e:{ex_id}")]
        for ex_id, name in chunk
    ]
    nav: list[InlineKeyboardButton] = []
    if page > 0:
        nav.append(InlineKeyboardButton(text="⬅️", callback_data=f"coach:expage:{page - 1}"))
    if start + per_page < len(items):
        nav.append(InlineKeyboardButton(text="➡️", callback_data=f"coach:expage:{page + 1}"))
    if nav:
        rows.append(nav)
    rows.append([InlineKeyboardButton(text=ui.BTN_BACK, callback_data="coach:menu")])
    return InlineKeyboardMarkup(inline_keyboard=rows)
