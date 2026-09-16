from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.types import Message
from sqlalchemy import select
from sqlalchemy.orm import selectinload

from app import ui_copy as ui
from app.config import get_settings
from app.db.models import ScheduleDay, WorkoutTemplate
from app.db.session import SessionLocal
from app.filters import PrivateChat
from app.keyboards import main_menu
from app.services.reminders import WEEKDAY_NAMES, get_template_for_weekday
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


@router.message(Command("program"))
@router.message(F.text == ui.BTN_PROGRAM)
async def show_program(message: Message) -> None:
    user = await _ensure_onboarded(message)
    if not user:
        return

    async with SessionLocal() as session:
        schedule = (
            await session.execute(
                select(ScheduleDay).options(
                    selectinload(ScheduleDay.template).selectinload(WorkoutTemplate.exercises)
                )
            )
        ).scalars().all()
        templates = (
            await session.execute(
                select(WorkoutTemplate).options(selectinload(WorkoutTemplate.exercises))
            )
        ).scalars().all()
        from app.services.program_review import load_stored_advice

        advice_raw = await load_stored_advice(session)

    if not templates:
        await message.answer(
            f"Программа ещё не задана. Админ: кнопка «{ui.BTN_ADMIN}».",
            reply_markup=main_menu(show_admin=can_open_admin(user)),
        )
        return

    by_day = {s.weekday: s.template for s in schedule}
    lines = [f"{ui.b(ui.BTN_PROGRAM)} График недели (read-only):"]
    for weekday in range(7):
        tpl = by_day.get(weekday)
        label = ui.b(tpl.name) if tpl else "отдых"
        lines.append(f"• {ui.b(WEEKDAY_NAMES[weekday])}: {label}")

    from app.services.program_review import format_advice_for_program_card

    advice_block = format_advice_for_program_card(advice_raw)
    if advice_block:
        lines.append("")
        lines.append(advice_block)

    lines.append("")
    lines.append(f"{ui.b('Шаблоны:')}")
    for tpl in templates:
        lines.append("")
        lines.append(_format_template(tpl))

    await message.answer(
        "\n".join(lines),
        reply_markup=main_menu(show_admin=can_open_admin(user)),
    )


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
