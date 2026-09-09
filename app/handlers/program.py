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
from app.services.reminders import WEEKDAY_NAMES, get_template_for_weekday
from app.services.users import get_or_create_user

router = Router(name="program")
router.message.filter(PrivateChat())


async def _ensure_onboarded(message: Message) -> bool:
    if message.from_user is None:
        return False
    async with SessionLocal() as session:
        user = await get_or_create_user(
            session,
            message.from_user.id,
            message.from_user.full_name or "Athlete",
        )
        if not user.onboarding_done:
            await message.answer("Сначала /start.")
            return False
    return True


def _format_template(template: WorkoutTemplate) -> str:
    lines = [f"{ui.ICO_EXERCISE} {template.name} (#{template.hashtag})"]
    if not template.exercises:
        lines.append("  (упражнений пока нет)")
    for ex in template.exercises:
        lines.append(
            f"  {ex.position + 1}. {ex.name} — "
            f"{ex.target_sets}×{ex.target_reps_min}-{ex.target_reps_max}, "
            f"шаг {ex.weight_step:g} кг"
        )
    return "\n".join(lines)


@router.message(Command("program"))
@router.message(F.text == ui.BTN_PROGRAM)
async def show_program(message: Message) -> None:
    if not await _ensure_onboarded(message):
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

    if not templates:
        await message.answer(f"Программа ещё не задана. Админ: кнопка «{ui.BTN_ADMIN}».")
        return

    by_day = {s.weekday: s.template for s in schedule}
    lines = [f"{ui.BTN_PROGRAM} График недели (read-only):"]
    for weekday in range(7):
        tpl = by_day.get(weekday)
        label = tpl.name if tpl else "отдых"
        lines.append(f"• {WEEKDAY_NAMES[weekday]}: {label}")

    lines.append("")
    lines.append("Шаблоны:")
    for tpl in templates:
        lines.append("")
        lines.append(_format_template(tpl))

    await message.answer("\n".join(lines))


@router.message(Command("today"))
@router.message(F.text == ui.BTN_TODAY)
async def show_today(message: Message) -> None:
    if not await _ensure_onboarded(message):
        return

    settings = get_settings()
    today = datetime.now(ZoneInfo(settings.timezone)).date()
    weekday = today.weekday()

    async with SessionLocal() as session:
        template = await get_template_for_weekday(session, weekday)

    if not template:
        await message.answer(
            f"{ui.BTN_TODAY} {WEEKDAY_NAMES[weekday]} — по графику отдых / день не назначен."
        )
        return

    await message.answer(
        f"{ui.BTN_TODAY} {WEEKDAY_NAMES[weekday]} — {template.name}\n#{template.hashtag}\n\n"
        + _format_template(template)
        + f"\n\nЖми «{ui.BTN_WORKOUT}», чтобы логировать."
    )
