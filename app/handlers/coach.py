from __future__ import annotations

import asyncio

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from app import ui_copy as ui
from app.db.session import SessionLocal
from app.filters import PrivateChat
from app.keyboards import (
    coach_clear_confirm_kb,
    coach_exercises_kb,
    coach_menu_kb,
    coach_prompt_kb,
)
from app.services.coach_delivery import (
    format_nn_status_line,
    format_prompt_info_text,
    run_coach_and_reply,
)
from app.services.history import list_user_logged_exercises
from app.services.nn_client import NnStatus, get_nn_status, status_label
from app.services.nn_dialog import clear_dialog, dialog_turn_count
from app.services.users import get_or_create_user

router = Router(name="coach")
router.message.filter(PrivateChat())
router.callback_query.filter(PrivateChat())


async def _coach_home_text(status: NnStatus, *, turns: int = 0) -> str:
    line = format_nn_status_line(status)
    dialog_line = (
        f"Ваш диалог: {turns} реплик (только ваш)."
        if turns
        else "Ваш диалог пуст — только системный промпт."
    )
    if status == NnStatus.online:
        return (
            f"{ui.BTN_COACH}\n{line}\n{dialog_line}\n\n"
            "Выбери тип разбора. На CPU это может занять 1–2 минуты.\n"
            "Веса и программу бот сам не меняет — только текст.\n"
            f"«{ui.BTN_COACH_PROMPT}» — что уходит в модель заранее."
        )
    if status == NnStatus.disabled:
        return (
            f"{ui.BTN_COACH}\n{line}\n{dialog_line}\n\n"
            "Вызовы к нейросети отключены (NN_ENABLED=false).\n"
            "Логирование тренировок работает как обычно."
        )
    return (
        f"{ui.BTN_COACH}\n{line}\n{dialog_line}\n\n"
        "Сервис нейросети не запущен. Бот работает без него.\n"
        "На сервере: docker compose --profile nn up -d"
    )


async def _user_and_turns(telegram_id: int, full_name: str) -> tuple[int, int] | None:
    async with SessionLocal() as session:
        user = await get_or_create_user(session, telegram_id, full_name)
        if not user.onboarding_done:
            return None
        turns = await dialog_turn_count(session, user.id)
        return user.id, turns


@router.message(F.text == ui.BTN_COACH)
async def coach_menu_msg(message: Message, state: FSMContext) -> None:
    await state.clear()
    if message.from_user is None:
        return
    got = await _user_and_turns(
        message.from_user.id, message.from_user.full_name or "Athlete"
    )
    if got is None:
        await message.answer("Сначала /start — нужно пройти онбординг.")
        return
    _user_id, turns = got
    status = await get_nn_status(force=True)
    await message.answer(
        await _coach_home_text(status, turns=turns),
        reply_markup=coach_menu_kb(online=status == NnStatus.online, turns=turns),
    )


@router.callback_query(F.data == "coach:menu")
@router.callback_query(F.data == "coach:refresh")
async def coach_menu_cb(callback: CallbackQuery) -> None:
    if callback.message is None or callback.from_user is None:
        return
    from aiogram.exceptions import TelegramBadRequest

    got = await _user_and_turns(
        callback.from_user.id, callback.from_user.full_name or "Athlete"
    )
    turns = got[1] if got else 0
    status = await get_nn_status(force=True)
    text = await _coach_home_text(status, turns=turns)
    kb = coach_menu_kb(online=status == NnStatus.online, turns=turns)
    try:
        await callback.message.edit_text(text, reply_markup=kb)
        await callback.answer(f"Статус: {status_label(status)}")
    except TelegramBadRequest as exc:
        if "message is not modified" in str(exc):
            await callback.answer(f"Статус: {status_label(status)} (без изменений)")
        else:
            raise


@router.callback_query(F.data == "coach:prompt")
async def coach_prompt_cb(callback: CallbackQuery) -> None:
    if callback.message is None or callback.from_user is None:
        return
    got = await _user_and_turns(
        callback.from_user.id, callback.from_user.full_name or "Athlete"
    )
    turns = got[1] if got else 0
    text = await format_prompt_info_text(turns=turns)
    await callback.message.edit_text(text, reply_markup=coach_prompt_kb())
    await callback.answer()


@router.callback_query(F.data == "coach:clear")
async def coach_clear_ask(callback: CallbackQuery) -> None:
    if callback.message is None:
        return
    await callback.message.edit_text(
        f"{ui.ICO_NN} Очистить ваш диалог с нейросетью?\n\n"
        "Удалится история реплик. Останется только системный промпт "
        "(он подставляется заново при следующем запросе).\n"
        "Чужие диалоги и тренировки не трогаются.",
        reply_markup=coach_clear_confirm_kb(),
    )
    await callback.answer()


@router.callback_query(F.data == "coach:clearok")
async def coach_clear_ok(callback: CallbackQuery) -> None:
    if callback.message is None or callback.from_user is None:
        return
    async with SessionLocal() as session:
        user = await get_or_create_user(
            session,
            callback.from_user.id,
            callback.from_user.full_name or "Athlete",
        )
        deleted = await clear_dialog(session, user.id)
    status = await get_nn_status(force=True)
    await callback.message.edit_text(
        f"Диалог очищен ({deleted} реплик).\n\n"
        + await _coach_home_text(status, turns=0),
        reply_markup=coach_menu_kb(online=status == NnStatus.online, turns=0),
    )
    await callback.answer("Очищено")


async def _start_kind(callback: CallbackQuery, kind: str) -> None:
    if callback.from_user is None or callback.message is None:
        return
    status = await get_nn_status(force=True)
    if status != NnStatus.online:
        got = await _user_and_turns(
            callback.from_user.id, callback.from_user.full_name or "Athlete"
        )
        turns = got[1] if got else 0
        await callback.message.edit_text(
            await _coach_home_text(status, turns=turns),
            reply_markup=coach_menu_kb(online=False, turns=turns),
        )
        await callback.answer("Нейросеть недоступна", show_alert=True)
        return

    async with SessionLocal() as session:
        user = await get_or_create_user(
            session,
            callback.from_user.id,
            callback.from_user.full_name or "Athlete",
        )
        user_id = user.id

    await callback.message.edit_text(
        f"{ui.ICO_NN} Готовлю разбор ({kind})… Это может занять до пары минут."
    )
    await callback.answer()
    asyncio.create_task(
        run_coach_and_reply(
            bot=callback.bot,
            chat_id=callback.message.chat.id,
            user_id=user_id,
            kind=kind,
            waiting_message=callback.message,
        )
    )


@router.callback_query(F.data == "coach:week")
async def coach_week(callback: CallbackQuery) -> None:
    await _start_kind(callback, "week")


@router.callback_query(F.data == "coach:month")
async def coach_month(callback: CallbackQuery) -> None:
    await _start_kind(callback, "month")


async def _exercise_items(user_id: int) -> list[tuple[int, str]]:
    """Same pool as History: only exercises from finished workouts."""
    async with SessionLocal() as session:
        return await list_user_logged_exercises(session, user_id)


@router.callback_query(F.data == "coach:exlist")
@router.callback_query(F.data.startswith("coach:expage:"))
async def coach_ex_list(callback: CallbackQuery) -> None:
    if callback.from_user is None or callback.message is None or callback.data is None:
        return
    status = await get_nn_status()
    if status != NnStatus.online:
        got = await _user_and_turns(
            callback.from_user.id, callback.from_user.full_name or "Athlete"
        )
        turns = got[1] if got else 0
        await callback.message.edit_text(
            await _coach_home_text(status, turns=turns),
            reply_markup=coach_menu_kb(online=False, turns=turns),
        )
        await callback.answer("Нейросеть недоступна", show_alert=True)
        return

    page = 0
    if callback.data.startswith("coach:expage:"):
        try:
            page = int(callback.data.split(":")[-1])
        except ValueError:
            page = 0

    async with SessionLocal() as session:
        user = await get_or_create_user(
            session,
            callback.from_user.id,
            callback.from_user.full_name or "Athlete",
        )
        user_id = user.id

    items = await _exercise_items(user_id)
    if not items:
        await callback.message.edit_text(
            f"{ui.BTN_COACH_EXERCISE}\n\n"
            "Пока пусто — сюда попадают только упражнения из "
            f"завершённых тренировок (как в «{ui.BTN_HISTORY}»).\n\n"
            f"Дологируй подходы и нажми «{ui.BTN_FINISH_WORKOUT}». "
            "Отмена и сброс не считаются.",
            reply_markup=coach_menu_kb(online=True),
        )
        await callback.answer()
        return

    await callback.message.edit_text(
        f"{ui.BTN_COACH_EXERCISE}\n"
        "Из завершённых тренировок — выбери упражнение:",
        reply_markup=coach_exercises_kb(items, page=page),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("coach:e:"))
async def coach_exercise(callback: CallbackQuery) -> None:
    if callback.from_user is None or callback.message is None or callback.data is None:
        return
    try:
        ex_id = int(callback.data.split(":")[-1])
    except ValueError:
        await callback.answer("Некорректное упражнение")
        return

    status = await get_nn_status(force=True)
    if status != NnStatus.online:
        got = await _user_and_turns(
            callback.from_user.id, callback.from_user.full_name or "Athlete"
        )
        turns = got[1] if got else 0
        await callback.message.edit_text(
            await _coach_home_text(status, turns=turns),
            reply_markup=coach_menu_kb(online=False, turns=turns),
        )
        await callback.answer("Нейросеть недоступна", show_alert=True)
        return

    async with SessionLocal() as session:
        user = await get_or_create_user(
            session,
            callback.from_user.id,
            callback.from_user.full_name or "Athlete",
        )
        user_id = user.id

    items = await _exercise_items(user_id)
    name = next((n for i, n in items if i == ex_id), f"#{ex_id}")

    await callback.message.edit_text(
        f"{ui.ICO_NN} Совет по «{name}»… Это может занять до пары минут."
    )
    await callback.answer()
    asyncio.create_task(
        run_coach_and_reply(
            bot=callback.bot,
            chat_id=callback.message.chat.id,
            user_id=user_id,
            kind="exercise",
            focus_exercise_id=ex_id,
            focus_exercise_name=name,
            waiting_message=callback.message,
        )
    )
