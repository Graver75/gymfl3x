from __future__ import annotations

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from app.db.session import SessionLocal
from app.filters import PrivateChat
from app.keyboards import (
    history_back_kb,
    history_exercises_kb,
    history_home_kb,
    history_sessions_kb,
)
from app.services.history import (
    format_exercise_history,
    format_session_history,
    get_user_session,
    list_recent_sessions,
    list_user_exercise_names,
)
from app.services.users import get_or_create_user

router = Router(name="history")
router.message.filter(PrivateChat())
router.callback_query.filter(PrivateChat())


async def _user(message_or_cb) -> tuple | None:
    from_user = message_or_cb.from_user
    if from_user is None:
        return None
    async with SessionLocal() as session:
        user = await get_or_create_user(
            session,
            from_user.id,
            from_user.full_name or "Athlete",
        )
        if not user.onboarding_done:
            return None
        return user


@router.message(Command("history"))
@router.message(F.text == "История")
async def history_home(message: Message, state: FSMContext) -> None:
    await state.clear()
    user = await _user(message)
    if not user:
        await message.answer("Сначала /start.")
        return
    await message.answer(
        "Твоя история тренировок и упражнений.\n"
        "Программа общая, результаты — только твои.",
        reply_markup=history_home_kb(),
    )


@router.callback_query(F.data == "hist:home")
async def hist_home_cb(callback: CallbackQuery, state: FSMContext) -> None:
    if callback.message is None:
        return
    await state.clear()
    await callback.message.edit_text(
        "Твоя история тренировок и упражнений.\n"
        "Программа общая, результаты — только твои.",
        reply_markup=history_home_kb(),
    )
    await callback.answer()


@router.callback_query(F.data == "hist:sessions")
async def hist_sessions(callback: CallbackQuery, state: FSMContext) -> None:
    if callback.message is None or callback.from_user is None:
        return
    async with SessionLocal() as session:
        user = await get_or_create_user(
            session, callback.from_user.id, callback.from_user.full_name or "Athlete"
        )
        sessions = await list_recent_sessions(session, user.id, limit=12)
    await state.update_data(hist_names=[])
    await callback.message.edit_text(
        "Последние тренировки:",
        reply_markup=history_sessions_kb(sessions),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("hist:s:"))
async def hist_session_detail(callback: CallbackQuery) -> None:
    if callback.message is None or callback.from_user is None or callback.data is None:
        return
    session_id = int(callback.data.split(":")[-1])
    async with SessionLocal() as session:
        user = await get_or_create_user(
            session, callback.from_user.id, callback.from_user.full_name or "Athlete"
        )
        ws = await get_user_session(session, user.id, session_id)
        if not ws:
            await callback.answer("Не найдено", show_alert=True)
            return
        text = format_session_history(ws)
    await callback.message.edit_text(text, reply_markup=history_back_kb("hist:sessions"))
    await callback.answer()


@router.callback_query(F.data == "hist:exercises")
@router.callback_query(F.data.startswith("hist:ep:"))
async def hist_exercises(callback: CallbackQuery, state: FSMContext) -> None:
    if callback.message is None or callback.from_user is None:
        return
    page = 0
    if callback.data and callback.data.startswith("hist:ep:"):
        page = int(callback.data.split(":")[-1])
    async with SessionLocal() as session:
        user = await get_or_create_user(
            session, callback.from_user.id, callback.from_user.full_name or "Athlete"
        )
        names = await list_user_exercise_names(session, user.id)
    await state.update_data(hist_names=names)
    await callback.message.edit_text(
        "Упражнения, которые ты логировал:",
        reply_markup=history_exercises_kb(names, page),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("hist:e:"))
async def hist_exercise_detail(callback: CallbackQuery, state: FSMContext) -> None:
    if callback.message is None or callback.from_user is None or callback.data is None:
        return
    idx = int(callback.data.split(":")[-1])
    data = await state.get_data()
    names = data.get("hist_names") or []
    if idx < 0 or idx >= len(names):
        await callback.answer("Список устарел, открой снова", show_alert=True)
        return
    name = names[idx]
    async with SessionLocal() as session:
        user = await get_or_create_user(
            session, callback.from_user.id, callback.from_user.full_name or "Athlete"
        )
        text = await format_exercise_history(session, user.id, name, limit=8)
    page = idx // 8
    await callback.message.edit_text(text, reply_markup=history_back_kb(f"hist:ep:{page}"))
    await callback.answer()
