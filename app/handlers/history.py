from __future__ import annotations

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from app.config import get_settings
from app.db.session import SessionLocal
from app.filters import PrivateChat
from app.keyboards import (
    history_athletes_kb,
    history_back_kb,
    history_exercises_kb,
    history_home_kb,
    history_sessions_kb,
)
from app.services.history import (
    athlete_label,
    format_exercise_history,
    format_session_history,
    get_athlete,
    get_user_session,
    list_athletes,
    list_recent_sessions,
    list_user_exercise_names,
)
from app.services.users import get_or_create_user

router = Router(name="history")
router.message.filter(PrivateChat())
router.callback_query.filter(PrivateChat())


async def _viewer(message_or_cb):
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
        settings = get_settings()
        if from_user.id in settings.admin_telegram_ids and not user.is_admin:
            user.is_admin = True
            await session.commit()
        return user


async def _target_context(state: FSMContext, viewer) -> tuple[int, str, bool]:
    """Return (target_user_id, label, viewing_other)."""
    data = await state.get_data()
    target_id = data.get("hist_target_id")
    label = data.get("hist_target_label")
    if target_id and int(target_id) != viewer.id:
        if not viewer.is_admin:
            await state.update_data(hist_target_id=None, hist_target_label=None)
            return viewer.id, "ты", False
        if not label:
            async with SessionLocal() as session:
                athlete = await get_athlete(session, int(target_id))
                label = athlete_label(athlete) if athlete else f"#{target_id}"
            await state.update_data(hist_target_label=label)
        return int(target_id), str(label), True
    return viewer.id, "ты", False


def _home_text(*, viewing_other: bool, label: str) -> str:
    if viewing_other:
        return (
            f"История атлета: {label}\n"
            "Тренировки и упражнения — как у него в личке."
        )
    return (
        "Твоя история тренировок и упражнений.\n"
        "Программа общая, результаты — только твои."
    )


def _section_back(viewing_other: bool) -> str:
    return "hist:uhome" if viewing_other else "hist:home"


@router.message(Command("history"))
@router.message(F.text == "История")
async def history_home(message: Message, state: FSMContext) -> None:
    await state.clear()
    user = await _viewer(message)
    if not user:
        await message.answer("Сначала /start.")
        return
    await message.answer(
        _home_text(viewing_other=False, label="ты"),
        reply_markup=history_home_kb(is_admin=user.is_admin, viewing_other=False),
    )


@router.callback_query(F.data == "hist:home")
async def hist_home_cb(callback: CallbackQuery, state: FSMContext) -> None:
    if callback.message is None:
        return
    user = await _viewer(callback)
    if not user:
        await callback.answer("Сначала /start", show_alert=True)
        return
    await state.clear()
    await callback.message.edit_text(
        _home_text(viewing_other=False, label="ты"),
        reply_markup=history_home_kb(is_admin=user.is_admin, viewing_other=False),
    )
    await callback.answer()


@router.callback_query(F.data == "hist:uhome")
async def hist_user_home(callback: CallbackQuery, state: FSMContext) -> None:
    if callback.message is None:
        return
    viewer = await _viewer(callback)
    if not viewer:
        await callback.answer("Сначала /start", show_alert=True)
        return
    target_id, label, viewing_other = await _target_context(state, viewer)
    if not viewing_other:
        await hist_home_cb(callback, state)
        return
    await callback.message.edit_text(
        _home_text(viewing_other=True, label=label),
        reply_markup=history_home_kb(is_admin=False, viewing_other=True),
    )
    await callback.answer()


@router.callback_query(F.data == "hist:athletes")
@router.callback_query(F.data == "hist:athletes:adm")
@router.callback_query(F.data.startswith("hist:ap:"))
async def hist_athletes(callback: CallbackQuery, state: FSMContext) -> None:
    if callback.message is None or callback.from_user is None:
        return
    viewer = await _viewer(callback)
    if not viewer or not viewer.is_admin:
        await callback.answer("Только для админа", show_alert=True)
        return
    page = 0
    data = await state.get_data()
    back = data.get("hist_athletes_back") or "hist:home"
    if callback.data == "hist:athletes:adm":
        back = "adm:home"
    elif callback.data == "hist:athletes" and not data.get("hist_athletes_back"):
        back = "hist:home"
    if callback.data and callback.data.startswith("hist:ap:"):
        page = int(callback.data.split(":")[-1])
    async with SessionLocal() as session:
        users = await list_athletes(session)
    await state.update_data(
        hist_target_id=None,
        hist_target_label=None,
        hist_names=[],
        hist_athletes_back=back,
    )
    await callback.message.edit_text(
        "Чья история открыть?",
        reply_markup=history_athletes_kb(users, page=page, back=back),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("hist:au:"))
async def hist_pick_athlete(callback: CallbackQuery, state: FSMContext) -> None:
    if callback.message is None or callback.data is None:
        return
    viewer = await _viewer(callback)
    if not viewer or not viewer.is_admin:
        await callback.answer("Только для админа", show_alert=True)
        return
    target_id = int(callback.data.split(":")[-1])
    async with SessionLocal() as session:
        athlete = await get_athlete(session, target_id)
        if not athlete:
            await callback.answer("Пользователь не найден", show_alert=True)
            return
        label = athlete_label(athlete)
    await state.update_data(
        hist_target_id=target_id,
        hist_target_label=label,
        hist_names=[],
    )
    await callback.message.edit_text(
        _home_text(viewing_other=True, label=label),
        reply_markup=history_home_kb(is_admin=False, viewing_other=True),
    )
    await callback.answer()


@router.callback_query(F.data == "hist:sessions")
async def hist_sessions(callback: CallbackQuery, state: FSMContext) -> None:
    if callback.message is None:
        return
    viewer = await _viewer(callback)
    if not viewer:
        await callback.answer("Сначала /start", show_alert=True)
        return
    target_id, label, viewing_other = await _target_context(state, viewer)
    async with SessionLocal() as session:
        sessions = await list_recent_sessions(session, target_id, limit=12)
    title = f"Тренировки · {label}:" if viewing_other else "Последние тренировки:"
    await callback.message.edit_text(
        title,
        reply_markup=history_sessions_kb(sessions, back=_section_back(viewing_other)),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("hist:s:"))
async def hist_session_detail(callback: CallbackQuery, state: FSMContext) -> None:
    if callback.message is None or callback.data is None:
        return
    viewer = await _viewer(callback)
    if not viewer:
        await callback.answer("Сначала /start", show_alert=True)
        return
    target_id, label, viewing_other = await _target_context(state, viewer)
    session_id = int(callback.data.split(":")[-1])
    async with SessionLocal() as session:
        ws = await get_user_session(session, target_id, session_id)
        if not ws:
            await callback.answer("Не найдено", show_alert=True)
            return
        text = format_session_history(ws)
        if viewing_other:
            text = f"{label}\n{text}"
    await callback.message.edit_text(
        text,
        reply_markup=history_back_kb("hist:sessions"),
    )
    await callback.answer()


@router.callback_query(F.data == "hist:exercises")
@router.callback_query(F.data.startswith("hist:ep:"))
async def hist_exercises(callback: CallbackQuery, state: FSMContext) -> None:
    if callback.message is None:
        return
    viewer = await _viewer(callback)
    if not viewer:
        await callback.answer("Сначала /start", show_alert=True)
        return
    target_id, label, viewing_other = await _target_context(state, viewer)
    page = 0
    if callback.data and callback.data.startswith("hist:ep:"):
        page = int(callback.data.split(":")[-1])
    async with SessionLocal() as session:
        names = await list_user_exercise_names(session, target_id)
    await state.update_data(hist_names=names)
    title = (
        f"Упражнения · {label}:"
        if viewing_other
        else "Упражнения, которые ты логировал:"
    )
    await callback.message.edit_text(
        title,
        reply_markup=history_exercises_kb(
            names, page, back=_section_back(viewing_other)
        ),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("hist:e:"))
async def hist_exercise_detail(callback: CallbackQuery, state: FSMContext) -> None:
    if callback.message is None or callback.data is None:
        return
    viewer = await _viewer(callback)
    if not viewer:
        await callback.answer("Сначала /start", show_alert=True)
        return
    target_id, label, viewing_other = await _target_context(state, viewer)
    idx = int(callback.data.split(":")[-1])
    data = await state.get_data()
    names = data.get("hist_names") or []
    if idx < 0 or idx >= len(names):
        await callback.answer("Список устарел, открой снова", show_alert=True)
        return
    name = names[idx]
    async with SessionLocal() as session:
        text = await format_exercise_history(session, target_id, name, limit=8)
    if viewing_other:
        text = f"{label}\n{text}"
    page = idx // 8
    await callback.message.edit_text(
        text,
        reply_markup=history_back_kb(f"hist:ep:{page}"),
    )
    await callback.answer()
