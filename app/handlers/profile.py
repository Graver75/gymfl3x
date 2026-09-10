from __future__ import annotations

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from app import ui_copy as ui
from app.db.models import LogLevel, TrainingPhase
from app.db.session import SessionLocal
from app.filters import PrivateChat
from app.keyboards import main_menu, profile_kb, profile_reset_confirm_kb
from app.services.coach_delivery import format_nn_status_line
from app.services.metrics_log import log_body_weight
from app.services.nn_client import get_nn_status
from app.services.progression import PHASE_LABELS, phase_from_experience
from app.services.users import can_open_admin, get_or_create_user, reset_own_training_data
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


def _log_level_label(level: LogLevel | str | None) -> str:
    value = level.value if isinstance(level, LogLevel) else (level or LogLevel.minimal.value)
    name = ui.LOG_LEVEL_LABELS.get(value, value)
    hint = ui.LOG_LEVEL_HINTS.get(value, "")
    return f"{name} ({hint})" if hint else name


def _profile_text(user, *, nn_line: str | None = None) -> str:
    height = f"{user.height_cm:g} см" if user.height_cm else "—"
    months = user.experience_months if user.experience_months is not None else "—"
    if user.is_admin:
        role = "полный админ"
    elif user.is_program_admin:
        role = "админ программы"
    else:
        role = "нет"
    log_level = getattr(user, "log_level", None) or LogLevel.minimal
    lines = [
        f"{ui.b(ui.BTN_PROFILE)}",
        f"Имя: {ui.b(user.display_name)}",
        f"Код: {ui.b(user.short_code)}",
        f"Вес: {ui.b(f'{user.body_weight:g} кг')}",
        f"Рост: {ui.b(height)}",
        f"Стаж: {ui.b(f'{months} мес')}",
        f"Фаза: {ui.b(PHASE_LABELS[user.phase])}",
        f"Лог: {ui.b(_log_level_label(log_level))}",
        f"Админка: {ui.b(role)}",
    ]
    if nn_line:
        lines.append(nn_line)
    return "\n".join(lines)


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
        _profile_text(user, nn_line=nn_line) + "\n\nФаза и детализация лога — кнопки ниже.\n"
        "Вес: /weight · Стаж: /experience",
        reply_markup=profile_kb(user.phase, log_level),
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
        text = _profile_text(user, nn_line=await _nn_line())

    if callback.message:
        await callback.message.edit_text(
            text + "\n\nФаза и детализация лога — кнопки ниже.",
            reply_markup=profile_kb(phase, log_level),
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

    if callback.message:
        await callback.message.edit_text(
            text + "\n\nФаза и детализация лога — кнопки ниже.",
            reply_markup=profile_kb(phase, level),
        )
    await callback.answer(f"Лог: {ui.LOG_LEVEL_LABELS.get(level.value, level.value)}")


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
    await callback.message.edit_text(
        text + "\n\nФаза и детализация лога — кнопки ниже.\n"
        "Вес: /weight · Стаж: /experience",
        reply_markup=profile_kb(phase, log_level),
    )
    await callback.answer()


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
        show_admin = can_open_admin(user)
    await callback.message.edit_text(
        "Готово, данные обнулены.\n"
        f"Сессий: {stats['sessions']}, "
        f"состояний: {stats['exercise_states']}, "
        f"логов веса: {stats['body_weight_logs']}, "
        f"заметок: {stats['note_logs']}.\n\n"
        + text
        + "\n\nФаза и детализация лога — кнопки ниже.",
        reply_markup=profile_kb(phase, log_level),
    )
    await callback.message.answer(
        "Можно начинать с чистого листа.",
        reply_markup=main_menu(show_admin=show_admin),
    )
    await callback.answer("Обнулено")


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
    await message.answer("Стаж в месяцах (фаза пересчитается, если не менял вручную часто):")


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
        user.experience_months = months
        user.phase = phase_from_experience(months)
        await session.commit()
        phase = user.phase
        show_admin = can_open_admin(user)
    await state.clear()
    await message.answer(
        f"Стаж: {months} мес, фаза: {PHASE_LABELS[phase]}",
        reply_markup=main_menu(show_admin=show_admin),
    )
