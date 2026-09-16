from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.types import CallbackQuery, Message
from sqlalchemy import select

from app import ui_copy as ui
from app.config import get_settings
from app.db.models import WorkoutTemplate
from app.db.session import SessionLocal
from app.filters import PrivateChat
from app.keyboards import main_menu, program_advice_kb, program_card_kb
from app.services.reminders import WEEKDAY_NAMES, get_template_for_weekday
from app.services.telegram_safe import safe_edit_text
from app.services.users import can_open_admin, get_or_create_user

router = Router(name="program")
router.message.filter(PrivateChat())


async def _ensure_onboarded(message: Message):
    if message.from_user is None:
        return None
    async with SessionLocal() as session:
        user = await get_or_create_user(
            session,
            message.from_user.id,
            message.from_user.full_name or "Athlete",
        )
        if not user.onboarding_done:
            await message.answer("Сначала /start.")
            return None
        return user


def _format_template(template: WorkoutTemplate) -> str:
    lines = [f"{ui.ICO_EXERCISE} {ui.b(template.name)} (#{ui.esc(template.hashtag)})"]
    if not template.exercises:
        lines.append("  (упражнений пока нет)")
    for ex in template.exercises:
        lines.append(
            f"  {ex.position + 1}. {ui.b(ex.name)} — "
            f"{ex.target_sets}×{ex.target_reps_min}-{ex.target_reps_max}, "
            f"шаг {ex.weight_step:g} кг"
        )
    return "\n".join(lines)


async def _program_payload() -> tuple[str, int | None] | None:
    """Return (html, alarm_or_none) or None if no templates."""
    from app.services.program_review import build_program_card_text

    async with SessionLocal() as session:
        templates = (
            await session.execute(select(WorkoutTemplate).limit(1))
        ).scalars().first()
        if not templates:
            return None
        return await build_program_card_text(session)


@router.message(Command("program"))
@router.message(F.text == ui.BTN_PROGRAM)
async def show_program(message: Message) -> None:
    user = await _ensure_onboarded(message)
    if not user:
        return

    payload = await _program_payload()
    if payload is None:
        await message.answer(
            f"Программа ещё не задана. Админ: кнопка «{ui.BTN_ADMIN}».",
            reply_markup=main_menu(show_admin=can_open_admin(user)),
        )
        return

    text, alarm = payload
    await message.answer(text, reply_markup=program_card_kb(alarm))


@router.callback_query(F.data == "prog:ai")
async def prog_ai_open(callback: CallbackQuery) -> None:
    if callback.from_user is None or callback.message is None:
        return
    from app.services.program_review import (
        format_advice_view,
        load_alarm_level,
        load_stored_advice,
    )

    async with SessionLocal() as session:
        advice = await load_stored_advice(session)
        alarm = await load_alarm_level(session)
    if not advice:
        await callback.answer("Совета пока нет", show_alert=True)
        return
    text = format_advice_view(advice, alarm_level=alarm)
    if len(text) > 4000:
        text = text[:3990] + "…"
    await safe_edit_text(
        callback.message,
        text,
        reply_markup=program_advice_kb(),
    )
    await callback.answer()


@router.callback_query(F.data == "prog:back")
async def prog_ai_back(callback: CallbackQuery) -> None:
    if callback.from_user is None or callback.message is None:
        return
    payload = await _program_payload()
    if payload is None:
        await callback.answer("Программа пуста", show_alert=True)
        return
    text, alarm = payload
    await safe_edit_text(
        callback.message,
        text,
        reply_markup=program_card_kb(alarm),
    )
    await callback.answer()


@router.message(Command("today"))
@router.message(F.text == ui.BTN_TODAY)
async def show_today(message: Message) -> None:
    user = await _ensure_onboarded(message)
    if not user:
        return

    settings = get_settings()
    today = datetime.now(ZoneInfo(settings.timezone)).date()
    weekday = today.weekday()

    async with SessionLocal() as session:
        template = await get_template_for_weekday(session, weekday)

    kb = main_menu(show_admin=can_open_admin(user))
    if not template:
        await message.answer(
            f"{ui.b(ui.BTN_TODAY)} {ui.b(WEEKDAY_NAMES[weekday])} — "
            "по графику отдых / день не назначен.",
            reply_markup=kb,
        )
        return

    await message.answer(
        f"{ui.b(ui.BTN_TODAY)} {ui.b(WEEKDAY_NAMES[weekday])} — {ui.b(template.name)}\n"
        f"#{ui.esc(template.hashtag)}\n\n"
        + _format_template(template)
        + f"\n\nЖми «{ui.b(ui.BTN_WORKOUT)}», чтобы логировать.",
        reply_markup=kb,
    )
