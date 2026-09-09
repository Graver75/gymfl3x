from __future__ import annotations

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from app import ui_copy as ui
from app.config import get_settings
from app.db.session import SessionLocal
from app.filters import PrivateChat
from app.keyboards import (
    history_athletes_kb,
    history_back_kb,
    history_delete_session_kb,
    history_edit_set_kb,
    history_edit_sets_kb,
    history_exercises_kb,
    history_home_kb,
    history_session_detail_kb,
    history_sessions_kb,
)
from app.services.history import (
    athlete_label,
    delete_workout_session,
    format_athlete_week,
    format_exercise_history,
    format_session_history,
    get_athlete,
    get_user_session,
    list_athletes,
    list_recent_sessions,
    list_user_exercise_names,
    session_notes,
)
from app.db.models import SessionSet
from app.states import EditSessionSG
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
            f"{ui.ICO_HISTORY} История атлета: {label}\n"
            "Тренировки и упражнения — как у него в личке."
        )
    return (
        f"{ui.ICO_HISTORY} Твоя история тренировок и упражнений.\n"
        "Программа общая, результаты — только твои."
    )


def _section_back(viewing_other: bool) -> str:
    return "hist:uhome" if viewing_other else "hist:home"


@router.message(Command("history"))
@router.message(F.text == ui.BTN_HISTORY)
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
    # Avoid matching hist:sdel / hist:sdelok
    if callback.data.startswith("hist:sdel"):
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
        notes = await session_notes(session, session_id)
        text = format_session_history(ws, notes=notes)
        if viewing_other:
            text = f"{label}\n{text}"
    can_edit = (not viewing_other) or viewer.is_admin
    await callback.message.edit_text(
        text,
        reply_markup=history_session_detail_kb(
            session_id, can_edit=can_edit, back="hist:sessions"
        ),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("hist:sdel:"))
async def hist_delete_session_ask(callback: CallbackQuery, state: FSMContext) -> None:
    if callback.message is None or callback.data is None:
        return
    if ":sdelok:" in callback.data:
        return
    viewer = await _viewer(callback)
    if not viewer:
        await callback.answer("Сначала /start", show_alert=True)
        return
    target_id, label, viewing_other = await _target_context(state, viewer)
    can_edit = (not viewing_other) or viewer.is_admin
    if not can_edit:
        await callback.answer("Нет доступа", show_alert=True)
        return
    session_id = int(callback.data.split(":")[-1])
    async with SessionLocal() as session:
        ws = await get_user_session(session, target_id, session_id)
        if not ws:
            await callback.answer("Не найдено", show_alert=True)
            return
        title = ws.template.name if ws.template else "Тренировка"
        when = ui.format_user_date(ws.session_date)
    who = f" атлета {label}" if viewing_other else ""
    await callback.message.edit_text(
        f"Удалить тренировку{who}?\n"
        f"{title} · {when}\n\n"
        "Удалятся все подходы и связанные заметки этой сессии "
        "(данные для нейросети тоже). Вес тела и текущие рабочие веса упражнений останутся.",
        reply_markup=history_delete_session_kb(session_id),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("hist:sdelok:"))
async def hist_delete_session_ok(callback: CallbackQuery, state: FSMContext) -> None:
    if callback.message is None or callback.data is None:
        return
    viewer = await _viewer(callback)
    if not viewer:
        await callback.answer("Сначала /start", show_alert=True)
        return
    target_id, label, viewing_other = await _target_context(state, viewer)
    can_edit = (not viewing_other) or viewer.is_admin
    if not can_edit:
        await callback.answer("Нет доступа", show_alert=True)
        return
    session_id = int(callback.data.split(":")[-1])
    async with SessionLocal() as session:
        ok = await delete_workout_session(
            session, owner_user_id=target_id, session_id=session_id
        )
        sessions = await list_recent_sessions(session, target_id, limit=12) if ok else []
    if not ok:
        await callback.answer("Уже удалено", show_alert=True)
        return
    title = f"Тренировки · {label}:" if viewing_other else "Последние тренировки:"
    await callback.message.edit_text(
        f"Тренировка удалена.\n\n{title}",
        reply_markup=history_sessions_kb(sessions, back=_section_back(viewing_other)),
    )
    await callback.answer("Удалено")


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


@router.callback_query(F.data == "hist:week")
async def hist_week(callback: CallbackQuery, state: FSMContext) -> None:
    if callback.message is None:
        return
    viewer = await _viewer(callback)
    if not viewer or not viewer.is_admin:
        await callback.answer("Только для админа", show_alert=True)
        return
    target_id, label, viewing_other = await _target_context(state, viewer)
    if not viewing_other:
        await callback.answer("Сначала выбери атлета", show_alert=True)
        return
    async with SessionLocal() as session:
        text = await format_athlete_week(session, target_id, days=7)
    await callback.message.edit_text(text, reply_markup=history_back_kb("hist:uhome"))
    await callback.answer()


@router.callback_query(F.data == "hist:editlast")
async def hist_edit_last(callback: CallbackQuery, state: FSMContext) -> None:
    if callback.message is None:
        return
    viewer = await _viewer(callback)
    if not viewer:
        await callback.answer("Сначала /start", show_alert=True)
        return
    target_id, _, viewing_other = await _target_context(state, viewer)
    if viewing_other and not viewer.is_admin:
        await callback.answer("Нет доступа", show_alert=True)
        return
    async with SessionLocal() as session:
        sessions = await list_recent_sessions(session, target_id, limit=1)
        if not sessions:
            await callback.answer("Нет завершённых тренировок", show_alert=True)
            return
        ws = sessions[0]
    await state.update_data(edit_session_id=ws.id, hist_target_id=target_id)
    await _show_edit_sets(callback, state, ws.id)


async def _show_edit_sets(callback: CallbackQuery, state: FSMContext, session_id: int) -> None:
    viewer = await _viewer(callback)
    target_id, _, _ = await _target_context(state, viewer)
    async with SessionLocal() as session:
        ws = await get_user_session(session, target_id, session_id)
        if not ws:
            await callback.answer("Не найдено", show_alert=True)
            return
        sets = list(ws.sets)
    await state.set_state(EditSessionSG.pick_set)
    await state.update_data(edit_session_id=session_id)
    await callback.message.edit_text(
        "Выбери подход для правки:",
        reply_markup=history_edit_sets_kb(sets, session_id),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("hist:edit:"))
async def hist_edit_session(callback: CallbackQuery, state: FSMContext) -> None:
    if callback.message is None or callback.data is None:
        return
    viewer = await _viewer(callback)
    if not viewer:
        return
    session_id = int(callback.data.split(":")[-1])
    target_id, _, viewing_other = await _target_context(state, viewer)
    if viewing_other and not viewer.is_admin:
        await callback.answer("Нет доступа", show_alert=True)
        return
    # ensure target owns session
    async with SessionLocal() as session:
        ws = await get_user_session(session, target_id if viewing_other else viewer.id, session_id)
        if not ws and not viewing_other:
            ws = await get_user_session(session, viewer.id, session_id)
        if not ws:
            await callback.answer("Не найдено", show_alert=True)
            return
        await state.update_data(hist_target_id=ws.user_id)
    await _show_edit_sets(callback, state, session_id)


@router.callback_query(F.data.startswith("hist:es:"))
async def hist_edit_set_menu(callback: CallbackQuery, state: FSMContext) -> None:
    if callback.message is None or callback.data is None:
        return
    set_id = int(callback.data.split(":")[-1])
    data = await state.get_data()
    session_id = data.get("edit_session_id")
    if not session_id:
        await callback.answer("Открой правку снова", show_alert=True)
        return
    await state.update_data(edit_set_id=set_id)
    await callback.message.edit_text(
        "Что изменить?",
        reply_markup=history_edit_set_kb(set_id, int(session_id)),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("hist:edel:"))
async def hist_delete_set(callback: CallbackQuery, state: FSMContext) -> None:
    if callback.message is None or callback.data is None:
        return
    set_id = int(callback.data.split(":")[-1])
    viewer = await _viewer(callback)
    target_id, _, _ = await _target_context(state, viewer)
    data = await state.get_data()
    session_id = int(data.get("edit_session_id") or 0)
    async with SessionLocal() as session:
        row = await session.get(SessionSet, set_id)
        if not row or row.session_id != session_id:
            await callback.answer("Не найдено", show_alert=True)
            return
        ws = await get_user_session(session, target_id, session_id)
        if not ws:
            await callback.answer("Нет доступа", show_alert=True)
            return
        await session.delete(row)
        await session.commit()
    await callback.answer("Удалил")
    await _show_edit_sets(callback, state, session_id)


@router.callback_query(F.data.startswith("hist:ew:"))
async def hist_edit_weight_start(callback: CallbackQuery, state: FSMContext) -> None:
    if callback.message is None or callback.data is None:
        return
    set_id = int(callback.data.split(":")[-1])
    await state.set_state(EditSessionSG.edit_weight)
    await state.update_data(edit_set_id=set_id)
    await callback.message.answer("Новый вес числом, например 52.5")
    await callback.answer()


@router.callback_query(F.data.startswith("hist:er:"))
async def hist_edit_reps_start(callback: CallbackQuery, state: FSMContext) -> None:
    if callback.message is None or callback.data is None:
        return
    set_id = int(callback.data.split(":")[-1])
    await state.set_state(EditSessionSG.edit_reps)
    await state.update_data(edit_set_id=set_id)
    await callback.message.answer("Новые повторения целым числом")
    await callback.answer()


@router.message(EditSessionSG.edit_weight)
async def hist_edit_weight_save(message: Message, state: FSMContext) -> None:
    try:
        weight = float((message.text or "").replace(",", "."))
        if weight < 0 or weight > 500:
            raise ValueError
    except ValueError:
        await message.answer("Число кг, например 40.")
        return
    data = await state.get_data()
    set_id = data.get("edit_set_id")
    session_id = data.get("edit_session_id")
    viewer = await _viewer(message)
    target_id, _, _ = await _target_context(state, viewer)
    async with SessionLocal() as session:
        row = await session.get(SessionSet, set_id)
        if not row:
            await message.answer("Подход не найден.")
            return
        row.weight = weight
        row.volume = max(int(row.reps), 1) * weight * max(int(row.sets_count), 1)
        await session.commit()
    await state.set_state(EditSessionSG.pick_set)
    await message.answer(f"Вес обновлён: {weight:g} кг. Открой список подходов кнопкой ниже или /history.")
    # re-show via a fake - send edit sets as new message
    async with SessionLocal() as session:
        ws = await get_user_session(session, target_id, int(session_id))
        sets = list(ws.sets) if ws else []
    await message.answer(
        "Выбери подход для правки:",
        reply_markup=history_edit_sets_kb(sets, int(session_id)),
    )


@router.message(EditSessionSG.edit_reps)
async def hist_edit_reps_save(message: Message, state: FSMContext) -> None:
    try:
        reps = int((message.text or "").strip())
        if reps < 0 or reps > 200:
            raise ValueError
    except ValueError:
        await message.answer("Целое число повторений.")
        return
    data = await state.get_data()
    set_id = data.get("edit_set_id")
    session_id = data.get("edit_session_id")
    viewer = await _viewer(message)
    target_id, _, _ = await _target_context(state, viewer)
    async with SessionLocal() as session:
        row = await session.get(SessionSet, set_id)
        if not row:
            await message.answer("Подход не найден.")
            return
        row.reps = reps
        row.volume = max(reps, 1) * float(row.weight) * max(int(row.sets_count), 1)
        await session.commit()
    await state.set_state(EditSessionSG.pick_set)
    async with SessionLocal() as session:
        ws = await get_user_session(session, target_id, int(session_id))
        sets = list(ws.sets) if ws else []
    await message.answer(
        f"Повторы обновлены: {reps}.",
        reply_markup=history_edit_sets_kb(sets, int(session_id)),
    )
