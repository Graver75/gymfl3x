from __future__ import annotations

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from app import ui_copy as ui
from app.db.models import LogLevel, TrainingPhase
from app.db.session import SessionLocal
from app.filters import PrivateChat
from app.keyboards import main_menu, profile_ai_kb, profile_kb, profile_reset_confirm_kb
from app.services.coach_delivery import format_nn_status_line
from app.services.metrics_log import log_body_weight
from app.services.nn_client import get_nn_status
from app.services.progression import PHASE_LABELS, phase_from_experience
from app.services.strength_levels import profile_progress_lines
from app.services.users import can_open_admin, effective_experience_months, get_or_create_user, reset_own_training_data, set_experience_months
from app.states import ProfileSG

router = Router(name="profile")
router.message.filter(PrivateChat())
router.callback_query.filter(PrivateChat())


async def _require_user(message: Message):
    if message.from_user is None:
        return None
    async with SessionLocal() as session:
        user = await get_or_create_user(
            session,
            message.from_user.id,
            message.from_user.full_name or "Athlete",
        )
        if not user.onboarding_done:
            await message.answer("Сначала /start — нужно пройти онбординг.")
            return None
        return user


async def _require_user_cb(callback: CallbackQuery):
    if callback.from_user is None:
        return None
    async with SessionLocal() as session:
        user = await get_or_create_user(
            session,
            callback.from_user.id,
            callback.from_user.full_name or "Athlete",
        )
        if not user.onboarding_done:
            await callback.answer("Сначала /start", show_alert=True)
            return None
        return user


def _log_level_label(level: LogLevel | str | None) -> str:
    value = level.value if isinstance(level, LogLevel) else (level or LogLevel.minimal.value)
    name = ui.LOG_LEVEL_LABELS.get(value, value)
    hint = ui.LOG_LEVEL_HINTS.get(value, "")
    return f"{name} ({hint})" if hint else name


def _profile_text(user, *, nn_line: str | None = None) -> str:
    height = f"{user.height_cm:g} см" if user.height_cm else "—"
    exp = effective_experience_months(user)
    months = exp if exp is not None else "—"
    if user.is_admin:
        role = "полный админ"
    elif user.is_program_admin:
        role = "админ программы"
    else:
        role = "нет"
    log_level = getattr(user, "log_level", None) or LogLevel.minimal
    sex = getattr(user, "sex", None)
    sex_label = "Ж" if sex == "female" else ("М" if sex == "male" else "не указан")
    age = getattr(user, "age", None)
    age_s = str(age) if age else "—"
    lines = [
        f"{ui.b(ui.BTN_PROFILE)}",
        f"Имя: {ui.b(user.display_name)}",
        f"Код: {ui.b(user.short_code)}",
        f"Возраст: {ui.b(age_s)}",
        f"Вес: {ui.b(f'{user.body_weight:g} кг') if user.body_weight else ui.b('—')}",
        f"Рост: {ui.b(height)}",
        f"Стаж: {ui.b(f'{months} мес')}",
        f"Пол (для уровней): {ui.b(sex_label)}",
        f"Фаза: {ui.b(PHASE_LABELS[user.phase])}",
        f"Лог: {ui.b(_log_level_label(log_level))}",
        f"Админка: {ui.b(role)}",
    ]
    if nn_line:
        lines.append(nn_line)
    sess = bool(getattr(user, "ai_session_enabled", True))
    week = bool(getattr(user, "ai_week_enabled", True))
    dest = getattr(user, "ai_dest", None) or "both"
    dest_label = ui.AI_DEST_LABELS.get(dest, dest)
    lines.append(
        f"ИИ: тренировка {'вкл' if sess else 'выкл'} · "
        f"неделя {'вкл' if week else 'выкл'} · {dest_label}"
    )
    return "\n".join(lines)


def _ai_settings_text(user) -> str:
    sess = bool(getattr(user, "ai_session_enabled", True))
    week = bool(getattr(user, "ai_week_enabled", True))
    dest = getattr(user, "ai_dest", None) or "both"
    return (
        f"{ui.BTN_PROFILE_AI}\n\n"
        f"Разбор тренировки (после зала): {'вкл' if sess else 'выкл'}\n"
        f"Недельный разбор: {'вкл' if week else 'выкл'}\n"
        f"Куда слать: {ui.AI_DEST_LABELS.get(dest, dest)}\n\n"
        "Общий чат видят все участники — включай group/both только если ок.\n"
        "В общий чат разборы уходят одной сводкой (вечер / воскресенье), не по одному."
    )


async def _nn_line() -> str:
    return format_nn_status_line(await get_nn_status())


@router.message(Command("profile"))
@router.message(F.text == ui.BTN_PROFILE)
async def show_profile(message: Message, state: FSMContext) -> None:
    await state.clear()
    user = await _require_user(message)
    if not user:
        return
    log_level = getattr(user, "log_level", None) or LogLevel.minimal
    nn_line = await _nn_line()
    await message.answer(
        _profile_text(user, nn_line=nn_line) + "\n\nФаза, пол и детализация лога — кнопки ниже.\n"
        "Имя / код / возраст — кнопки. Вес: /weight · Стаж: /experience",
        reply_markup=main_menu(show_admin=can_open_admin(user)),
    )
    await message.answer(
        "Настройки профиля:",
        reply_markup=profile_kb(user.phase, log_level, sex=getattr(user, "sex", None)),
    )


@router.callback_query(F.data.startswith("profile:phase:"))
async def set_phase(callback: CallbackQuery) -> None:
    if callback.from_user is None or callback.data is None:
        return
    phase_value = callback.data.split(":")[-1]
    try:
        phase = TrainingPhase(phase_value)
    except ValueError:
        await callback.answer("Неизвестная фаза")
        return

    async with SessionLocal() as session:
        user = await get_or_create_user(
            session,
            callback.from_user.id,
            callback.from_user.full_name or "Athlete",
        )
        user.phase = phase
        await session.commit()
        log_level = getattr(user, "log_level", None) or LogLevel.minimal
        sex = getattr(user, "sex", None)
        text = _profile_text(user, nn_line=await _nn_line())

    if callback.message:
        await callback.message.edit_text(
            text + "\n\nФаза, пол и детализация лога — кнопки ниже.",
            reply_markup=profile_kb(phase, log_level, sex=sex),
        )
    await callback.answer("Фаза сохранена")


@router.callback_query(F.data.startswith("profile:log:"))
async def set_log_level(callback: CallbackQuery) -> None:
    if callback.from_user is None or callback.data is None:
        return
    raw = callback.data.split(":")[-1]
    try:
        level = LogLevel(raw)
    except ValueError:
        await callback.answer("Неизвестный уровень")
        return

    async with SessionLocal() as session:
        user = await get_or_create_user(
            session,
            callback.from_user.id,
            callback.from_user.full_name or "Athlete",
        )
        user.log_level = level
        await session.commit()
        text = _profile_text(user, nn_line=await _nn_line())
        phase = user.phase
        sex = getattr(user, "sex", None)

    if callback.message:
        await callback.message.edit_text(
            text + "\n\nФаза, пол и детализация лога — кнопки ниже.",
            reply_markup=profile_kb(phase, level, sex=sex),
        )
    await callback.answer(f"Лог: {ui.LOG_LEVEL_LABELS.get(level.value, level.value)}")


@router.callback_query(F.data.startswith("profile:sex:"))
async def set_sex(callback: CallbackQuery) -> None:
    if callback.from_user is None or callback.data is None or callback.message is None:
        return
    sex = callback.data.split(":")[-1]
    if sex not in {"male", "female"}:
        await callback.answer("Некорректно")
        return
    async with SessionLocal() as session:
        user = await get_or_create_user(
            session,
            callback.from_user.id,
            callback.from_user.full_name or "Athlete",
        )
        user.sex = sex
        await session.commit()
        log_level = getattr(user, "log_level", None) or LogLevel.minimal
        text = _profile_text(user, nn_line=await _nn_line())
        phase = user.phase
    await callback.message.edit_text(
        text + "\n\nФаза, пол и детализация лога — кнопки ниже.",
        reply_markup=profile_kb(phase, log_level, sex=sex),
    )
    await callback.answer("Пол сохранён")


@router.callback_query(F.data == "profile:progress")
async def profile_progress(callback: CallbackQuery) -> None:
    if callback.from_user is None or callback.message is None:
        return
    async with SessionLocal() as session:
        user = await get_or_create_user(
            session,
            callback.from_user.id,
            callback.from_user.full_name or "Athlete",
        )
        if not user.onboarding_done:
            await callback.answer("Сначала /start", show_alert=True)
            return
        text = await profile_progress_lines(session, user)
    if len(text) > 4000:
        text = text[:3990] + "…"
    from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton

    await callback.message.edit_text(
        text,
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text=ui.BTN_BACK, callback_data="profile:home")]
            ]
        ),
    )
    await callback.answer()


@router.callback_query(F.data == "profile:home")
async def profile_home_cb(callback: CallbackQuery) -> None:
    if callback.from_user is None or callback.message is None:
        return
    async with SessionLocal() as session:
        user = await get_or_create_user(
            session,
            callback.from_user.id,
            callback.from_user.full_name or "Athlete",
        )
        if not user.onboarding_done:
            await callback.answer("Сначала /start", show_alert=True)
            return
        log_level = getattr(user, "log_level", None) or LogLevel.minimal
        text = _profile_text(user, nn_line=await _nn_line())
        phase = user.phase
        sex = getattr(user, "sex", None)
    await callback.message.edit_text(
        text + "\n\nФаза, пол и детализация лога — кнопки ниже.\n"
        "Вес: /weight · Стаж: /experience",
        reply_markup=profile_kb(phase, log_level, sex=sex),
    )
    await callback.answer()


@router.callback_query(F.data == "profile:ai")
async def profile_ai_menu(callback: CallbackQuery) -> None:
    if callback.from_user is None or callback.message is None:
        return
    async with SessionLocal() as session:
        user = await get_or_create_user(
            session,
            callback.from_user.id,
            callback.from_user.full_name or "Athlete",
        )
        if not user.onboarding_done:
            await callback.answer("Сначала /start", show_alert=True)
            return
        text = _ai_settings_text(user)
        sess = bool(getattr(user, "ai_session_enabled", True))
        week = bool(getattr(user, "ai_week_enabled", True))
        dest = getattr(user, "ai_dest", None) or "both"
    await callback.message.edit_text(
        text, reply_markup=profile_ai_kb(session_on=sess, week_on=week, dest=dest)
    )
    await callback.answer()


@router.callback_query(F.data == "profile:ai:sess")
async def profile_ai_toggle_session(callback: CallbackQuery) -> None:
    if callback.from_user is None or callback.message is None:
        return
    async with SessionLocal() as session:
        user = await get_or_create_user(
            session,
            callback.from_user.id,
            callback.from_user.full_name or "Athlete",
        )
        user.ai_session_enabled = not bool(getattr(user, "ai_session_enabled", True))
        await session.commit()
        text = _ai_settings_text(user)
        sess = bool(user.ai_session_enabled)
        week = bool(getattr(user, "ai_week_enabled", True))
        dest = getattr(user, "ai_dest", None) or "both"
    await callback.message.edit_text(
        text, reply_markup=profile_ai_kb(session_on=sess, week_on=week, dest=dest)
    )
    await callback.answer("Сохранено")


@router.callback_query(F.data == "profile:ai:week")
async def profile_ai_toggle_week(callback: CallbackQuery) -> None:
    if callback.from_user is None or callback.message is None:
        return
    async with SessionLocal() as session:
        user = await get_or_create_user(
            session,
            callback.from_user.id,
            callback.from_user.full_name or "Athlete",
        )
        user.ai_week_enabled = not bool(getattr(user, "ai_week_enabled", True))
        await session.commit()
        text = _ai_settings_text(user)
        sess = bool(getattr(user, "ai_session_enabled", True))
        week = bool(user.ai_week_enabled)
        dest = getattr(user, "ai_dest", None) or "both"
    await callback.message.edit_text(
        text, reply_markup=profile_ai_kb(session_on=sess, week_on=week, dest=dest)
    )
    await callback.answer("Сохранено")


@router.callback_query(F.data.startswith("profile:ai:dest:"))
async def profile_ai_set_dest(callback: CallbackQuery) -> None:
    if callback.from_user is None or callback.message is None or callback.data is None:
        return
    dest = callback.data.split(":")[-1]
    if dest not in {"dm", "group", "both"}:
        await callback.answer("?")
        return
    async with SessionLocal() as session:
        user = await get_or_create_user(
            session,
            callback.from_user.id,
            callback.from_user.full_name or "Athlete",
        )
        user.ai_dest = dest
        await session.commit()
        text = _ai_settings_text(user)
        sess = bool(getattr(user, "ai_session_enabled", True))
        week = bool(getattr(user, "ai_week_enabled", True))
    await callback.message.edit_text(
        text, reply_markup=profile_ai_kb(session_on=sess, week_on=week, dest=dest)
    )
    await callback.answer("Куда слать — сохранено")


@router.callback_query(F.data == "profile:reset")
async def profile_reset_ask(callback: CallbackQuery) -> None:
    if callback.message is None:
        return
    await callback.message.edit_text(
        "Обнулить только свои данные?\n\n"
        "Удалится:\n"
        "• все тренировки и подходы\n"
        "• автоподбор весов / состояния упражнений\n"
        "• история веса тела\n"
        "• заметки к упражнениям\n"
        "• диалог с нейросетью\n\n"
        "Останется: имя, код, текущий вес/рост/стаж/фаза, "
        "настройки лога и роли админа.\n"
        "Чужие данные и общая программа не трогаются.",
        reply_markup=profile_reset_confirm_kb(),
    )
    await callback.answer()


@router.callback_query(F.data == "profile:resetok")
async def profile_reset_ok(callback: CallbackQuery) -> None:
    if callback.from_user is None or callback.message is None:
        return
    async with SessionLocal() as session:
        user = await get_or_create_user(
            session,
            callback.from_user.id,
            callback.from_user.full_name or "Athlete",
        )
        if not user.onboarding_done:
            await callback.answer("Сначала /start", show_alert=True)
            return
        stats = await reset_own_training_data(session, user.id)
        log_level = getattr(user, "log_level", None) or LogLevel.minimal
        text = _profile_text(user, nn_line=await _nn_line())
        phase = user.phase
        sex = getattr(user, "sex", None)
        show_admin = can_open_admin(user)
    await callback.message.edit_text(
        "Готово, данные обнулены.\n"
        f"Сессий: {stats['sessions']}, "
        f"состояний: {stats['exercise_states']}, "
        f"логов веса: {stats['body_weight_logs']}, "
        f"заметок: {stats['note_logs']}.\n\n"
        + text
        + "\n\nФаза, пол и детализация лога — кнопки ниже.",
        reply_markup=profile_kb(phase, log_level, sex=sex),
    )
    await callback.message.answer(
        "Можно начинать с чистого листа.",
        reply_markup=main_menu(show_admin=show_admin),
    )
    await callback.answer("Обнулено")


@router.callback_query(F.data == "profile:edit:name")
async def profile_edit_name_start(callback: CallbackQuery, state: FSMContext) -> None:
    if callback.from_user is None or callback.message is None:
        return
    user = await _require_user_cb(callback)
    if not user:
        return
    await state.set_state(ProfileSG.edit_name)
    await callback.message.answer(f"Сейчас имя: {user.display_name}\nПришли новое имя:")
    await callback.answer()


@router.message(ProfileSG.edit_name)
async def profile_save_name(message: Message, state: FSMContext) -> None:
    if message.from_user is None:
        return
    name = (message.text or "").strip()
    if len(name) < 1:
        await message.answer("Нужно имя.")
        return
    async with SessionLocal() as session:
        user = await get_or_create_user(
            session,
            message.from_user.id,
            message.from_user.full_name or "Athlete",
        )
        user.display_name = name[:64]
        await session.commit()
        show_admin = can_open_admin(user)
        text = _profile_text(user, nn_line=await _nn_line())
        log_level = getattr(user, "log_level", None) or LogLevel.minimal
        phase = user.phase
        sex = getattr(user, "sex", None)
    await state.clear()
    await message.answer(
        f"Имя обновлено.\n\n{text}",
        reply_markup=main_menu(show_admin=show_admin),
    )
    await message.answer(
        "Настройки профиля:",
        reply_markup=profile_kb(phase, log_level, sex=sex),
    )


@router.callback_query(F.data == "profile:edit:code")
async def profile_edit_code_start(callback: CallbackQuery, state: FSMContext) -> None:
    if callback.from_user is None or callback.message is None:
        return
    user = await _require_user_cb(callback)
    if not user:
        return
    await state.set_state(ProfileSG.edit_code)
    await callback.message.answer(
        f"Сейчас код: {user.short_code}\nПришли новый код (1–4 символа):"
    )
    await callback.answer()


@router.message(ProfileSG.edit_code)
async def profile_save_code(message: Message, state: FSMContext) -> None:
    if message.from_user is None:
        return
    code = (message.text or "").strip().upper()
    if not (1 <= len(code) <= 4):
        await message.answer("Код 1–4 символа.")
        return
    from app.services.users import short_code_taken

    async with SessionLocal() as session:
        user = await get_or_create_user(
            session,
            message.from_user.id,
            message.from_user.full_name or "Athlete",
        )
        if await short_code_taken(session, code, exclude_user_id=user.id):
            await message.answer("Этот код уже занят. Выбери другой.")
            return
        user.short_code = code
        await session.commit()
        show_admin = can_open_admin(user)
        text = _profile_text(user, nn_line=await _nn_line())
        log_level = getattr(user, "log_level", None) or LogLevel.minimal
        phase = user.phase
        sex = getattr(user, "sex", None)
    await state.clear()
    await message.answer(
        f"Код обновлён: {code}\n\n{text}",
        reply_markup=main_menu(show_admin=show_admin),
    )
    await message.answer(
        "Настройки профиля:",
        reply_markup=profile_kb(phase, log_level, sex=sex),
    )


@router.callback_query(F.data == "profile:edit:age")
async def profile_edit_age_start(callback: CallbackQuery, state: FSMContext) -> None:
    if callback.from_user is None or callback.message is None:
        return
    user = await _require_user_cb(callback)
    if not user:
        return
    cur = getattr(user, "age", None)
    await state.set_state(ProfileSG.edit_age)
    await callback.message.answer(
        f"Сейчас возраст: {cur if cur else '—'}\nПришли возраст числом:"
    )
    await callback.answer()


@router.message(ProfileSG.edit_age)
async def profile_save_age(message: Message, state: FSMContext) -> None:
    if message.from_user is None:
        return
    try:
        age = int((message.text or "").strip())
        if not (10 <= age <= 100):
            raise ValueError
    except ValueError:
        await message.answer("Возраст числом от 10 до 100.")
        return
    async with SessionLocal() as session:
        user = await get_or_create_user(
            session,
            message.from_user.id,
            message.from_user.full_name or "Athlete",
        )
        user.age = age
        await session.commit()
        show_admin = can_open_admin(user)
        text = _profile_text(user, nn_line=await _nn_line())
        log_level = getattr(user, "log_level", None) or LogLevel.minimal
        phase = user.phase
        sex = getattr(user, "sex", None)
    await state.clear()
    await message.answer(
        f"Возраст обновлён: {age}\n\n{text}",
        reply_markup=main_menu(show_admin=show_admin),
    )
    await message.answer(
        "Настройки профиля:",
        reply_markup=profile_kb(phase, log_level, sex=sex),
    )


@router.message(Command("weight"))
async def cmd_weight(message: Message, state: FSMContext) -> None:
    user = await _require_user(message)
    if not user:
        return
    await state.set_state(ProfileSG.edit_weight)
    await message.answer("Новый вес тела в кг:")


@router.message(ProfileSG.edit_weight)
async def save_weight(message: Message, state: FSMContext) -> None:
    if message.from_user is None:
        return
    try:
        weight = float((message.text or "").replace(",", "."))
        if not (30 < weight < 300):
            raise ValueError
    except ValueError:
        await message.answer("Например 82.5")
        return
    async with SessionLocal() as session:
        user = await get_or_create_user(
            session,
            message.from_user.id,
            message.from_user.full_name or "Athlete",
        )
        user.body_weight = weight
        await log_body_weight(session, user.id, weight)
        await session.commit()
        show_admin = can_open_admin(user)
    await state.clear()
    await message.answer(
        f"Вес обновлён: {weight:g} кг",
        reply_markup=main_menu(show_admin=show_admin),
    )


@router.message(Command("experience"))
async def cmd_experience(message: Message, state: FSMContext) -> None:
    user = await _require_user(message)
    if not user:
        return
    await state.set_state(ProfileSG.edit_experience)
    cur = effective_experience_months(user)
    cur_s = f"{cur} мес" if cur is not None else "не указан"
    await message.answer(
        f"Текущий стаж: <b>{cur_s}</b> (растёт сам с месяцами).\n"
        "Пришли актуальное число месяцев — отсчёт продолжится с сегодня:"
    )


@router.message(ProfileSG.edit_experience)
async def save_experience(message: Message, state: FSMContext) -> None:
    if message.from_user is None:
        return
    try:
        months = int((message.text or "").strip())
        if months < 0 or months > 600:
            raise ValueError
    except ValueError:
        await message.answer("Целое число месяцев.")
        return
    async with SessionLocal() as session:
        user = await get_or_create_user(
            session,
            message.from_user.id,
            message.from_user.full_name or "Athlete",
        )
        set_experience_months(user, months)
        user.phase = phase_from_experience(months)
        await session.commit()
        phase = user.phase
        show_admin = can_open_admin(user)
    await state.clear()
    await message.answer(
        f"Стаж: {months} мес, фаза: {PHASE_LABELS[phase]}",
        reply_markup=main_menu(show_admin=show_admin),
    )
