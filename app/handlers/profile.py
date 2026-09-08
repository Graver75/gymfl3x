from __future__ import annotations

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from app.db.models import TrainingPhase
from app.db.session import SessionLocal
from app.filters import PrivateChat
from app.keyboards import main_menu, phase_kb
from app.services.progression import PHASE_LABELS, phase_from_experience
from app.services.users import get_or_create_user
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


def _profile_text(user) -> str:
    height = f"{user.height_cm:g} см" if user.height_cm else "—"
    months = user.experience_months if user.experience_months is not None else "—"
    return (
        f"Профиль\n"
        f"Имя: {user.display_name}\n"
        f"Код: {user.short_code}\n"
        f"Вес: {user.body_weight:g} кг\n"
        f"Рост: {height}\n"
        f"Стаж: {months} мес\n"
        f"Фаза: {PHASE_LABELS[user.phase]}\n"
        f"Админ: {'да' if user.is_admin else 'нет'}"
    )


@router.message(Command("profile"))
@router.message(F.text == "Профиль")
async def show_profile(message: Message, state: FSMContext) -> None:
    await state.clear()
    user = await _require_user(message)
    if not user:
        return
    await message.answer(
        _profile_text(user) + "\n\nСменить фазу — кнопки ниже.\n"
        "Чтобы обновить вес: /weight\n"
        "Стаж: /experience",
        reply_markup=phase_kb(user.phase),
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

    if callback.message:
        await callback.message.edit_text(
            f"Фаза: {PHASE_LABELS[phase]}",
            reply_markup=phase_kb(phase),
        )
    await callback.answer("Сохранено")


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
        await session.commit()
        is_admin = user.is_admin
    await state.clear()
    await message.answer(f"Вес обновлён: {weight:g} кг", reply_markup=main_menu(is_admin))


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
        is_admin = user.is_admin
    await state.clear()
    await message.answer(
        f"Стаж: {months} мес, фаза: {PHASE_LABELS[phase]}",
        reply_markup=main_menu(is_admin),
    )
