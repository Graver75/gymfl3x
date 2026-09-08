from __future__ import annotations

from datetime import date, datetime
from zoneinfo import ZoneInfo

from aiogram import Bot
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.config import Settings
from app.db.models import GroupChat, RecapSent, ScheduleDay, WorkoutTemplate
from app.db.session import SessionLocal
from app.services.recap import build_group_recap


WEEKDAY_NAMES = (
    "понедельник",
    "вторник",
    "среда",
    "четверг",
    "пятница",
    "суббота",
    "воскресенье",
)


async def get_template_for_weekday(session: AsyncSession, weekday: int) -> WorkoutTemplate | None:
    result = await session.execute(
        select(ScheduleDay)
        .where(ScheduleDay.weekday == weekday)
        .options(selectinload(ScheduleDay.template).selectinload(WorkoutTemplate.exercises))
    )
    day = result.scalar_one_or_none()
    return day.template if day else None


async def _already_sent(session: AsyncSession, chat_id: int, day: date, kind: str) -> bool:
    result = await session.execute(
        select(RecapSent).where(
            RecapSent.chat_id == chat_id,
            RecapSent.recap_date == day,
            RecapSent.kind == kind,
        )
    )
    return result.scalar_one_or_none() is not None


async def _mark_sent(session: AsyncSession, chat_id: int, day: date, kind: str) -> None:
    session.add(RecapSent(chat_id=chat_id, recap_date=day, kind=kind))
    await session.commit()


async def send_morning_reminders(bot: Bot, settings: Settings) -> None:
    tz = ZoneInfo(settings.timezone)
    now = datetime.now(tz)
    today = now.date()
    weekday = today.weekday()

    async with SessionLocal() as session:
        template = await get_template_for_weekday(session, weekday)
        if not template:
            return

        groups = (
            await session.execute(select(GroupChat).where(GroupChat.active.is_(True)))
        ).scalars().all()

        exercise_lines = "\n".join(
            f"• {ex.name} ({ex.target_sets}×{ex.target_reps_min}-{ex.target_reps_max})"
            for ex in template.exercises
        )
        me = await bot.get_me()
        deep_link = f"https://t.me/{me.username}?start=workout"
        kb = InlineKeyboardMarkup(
            inline_keyboard=[[InlineKeyboardButton(text="Открыть бота", url=deep_link)]]
        )
        text = (
            f"Сегодня {WEEKDAY_NAMES[weekday]} — {template.name}\n"
            f"#{template.hashtag}\n\n"
            f"{exercise_lines}\n\n"
            "Логируем в личке с ботом кнопками."
        )

        for group in groups:
            hour = group.reminder_hour if group.reminder_hour is not None else settings.reminder_hour
            if now.hour != hour:
                continue
            if await _already_sent(session, group.chat_id, today, "reminder"):
                continue
            try:
                await bot.send_message(group.chat_id, text, reply_markup=kb)
                await _mark_sent(session, group.chat_id, today, "reminder")
            except Exception:
                # Chat may have kicked the bot; keep going.
                continue


async def send_evening_recaps(bot: Bot, settings: Settings) -> None:
    tz = ZoneInfo(settings.timezone)
    now = datetime.now(tz)
    today = now.date()
    weekday = today.weekday()

    async with SessionLocal() as session:
        template = await get_template_for_weekday(session, weekday)
        if not template:
            return

        groups = (
            await session.execute(select(GroupChat).where(GroupChat.active.is_(True)))
        ).scalars().all()
        text = await build_group_recap(session, template, today)

        for group in groups:
            hour = group.recap_hour if group.recap_hour is not None else settings.recap_hour
            if now.hour != hour:
                continue
            if await _already_sent(session, group.chat_id, today, "recap"):
                continue
            try:
                await bot.send_message(group.chat_id, text)
                await _mark_sent(session, group.chat_id, today, "recap")
            except Exception:
                continue


async def maybe_send_live_recap(bot: Bot, session: AsyncSession, settings: Settings, day: date) -> None:
    """Optional: after someone finishes, update is not auto-spam; evening job handles it.
    Kept as hook for future 'all done' logic.
    """
    _ = (bot, session, settings, day)
