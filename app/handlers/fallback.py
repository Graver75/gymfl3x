"""Catch-all: restore reply menu for onboarded users on unknown private texts."""

from __future__ import annotations

from aiogram import Router
from aiogram.fsm.context import FSMContext
from aiogram.types import Message

from app import ui_copy as ui
from app.db.session import SessionLocal
from app.filters import PrivateChat
from app.keyboards import main_menu
from app.middlewares.menu_reset import MAIN_MENU_TEXTS
from app.services.users import can_open_admin, get_or_create_user

router = Router(name="fallback")
router.message.filter(PrivateChat())


@router.message()
async def restore_menu_fallback(message: Message, state: FSMContext) -> None:
    if message.from_user is None:
        return
    # Menu buttons have their own handlers; don't double-handle.
    if (message.text or "") in MAIN_MENU_TEXTS:
        return
    async with SessionLocal() as session:
        user = await get_or_create_user(
            session,
            message.from_user.id,
            message.from_user.full_name or "Athlete",
        )
        if not user.onboarding_done:
            await message.answer(
                f"Сначала пройди онбординг: нажми /start"
            )
            return
        show_admin = can_open_admin(user)
        name = user.display_name
    await state.clear()
    await message.answer(
        f"{ui.ICO_WAVE} {ui.b(name)}, я не понял сообщение.\n"
        "Жми кнопки меню внизу — /start не обязателен.",
        reply_markup=main_menu(show_admin=show_admin),
    )
