from __future__ import annotations

import logging
from datetime import date, datetime
from zoneinfo import ZoneInfo

from aiogram import Bot
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.config import Settings
from app.db.models import GroupChat, RecapSent, ScheduleDay, WorkoutTemplate
from app.db.session import SessionLocal
from app.services.broadcast_admin import (
    build_evening_fact,
    build_morning_text,
    send_with_divert,
)
from app import ui_copy as ui

logger = logging.getLogger("gymflex.reminders")

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

    async with SessionLocal() as session:
        text, kb = await build_morning_text(bot, session, today)
        if "нет дня в графике" in text:
            return

        groups = (
            await session.execute(select(GroupChat).where(GroupChat.active.is_(True)))
        ).scalars().all()

        for group in groups:
            hour = group.reminder_hour if group.reminder_hour is not None else settings.reminder_hour
            if now.hour != hour:
                continue
            if await _already_sent(session, group.chat_id, today, "reminder"):
                continue
            try:
                await send_with_divert(
                    bot,
                    session,
                    intended_chat_id=group.chat_id,
                    intended_label=group.title or str(group.chat_id),
                    text=text,
                    reply_markup=kb,
                )
                await _mark_sent(session, group.chat_id, today, "reminder")
            except Exception:
                logger.exception("morning reminder failed chat=%s", group.chat_id)
                continue


async def send_evening_recaps(bot: Bot, settings: Settings) -> None:
    tz = ZoneInfo(settings.timezone)
    now = datetime.now(tz)
    today = now.date()

    async with SessionLocal() as session:
        text, template = await build_evening_fact(session, today)
        if template is None:
            return

        groups = (
            await session.execute(select(GroupChat).where(GroupChat.active.is_(True)))
        ).scalars().all()

        for group in groups:
            hour = group.recap_hour if group.recap_hour is not None else settings.recap_hour
            if now.hour != hour:
                continue
            if await _already_sent(session, group.chat_id, today, "recap"):
                continue
            out = text
            try:
                from app.services.ai_digests import append_session_group_ai

                ai_block = await append_session_group_ai(
                    bot,
                    session,
                    group=group,
                    day=today,
                    template=template,
                    fact_text=text,
                )
                if ai_block:
                    out = f"{text}\n\n{ui.ICO_NN} Разбор ИИ\n{ai_block}"
                    if len(out) > 4000:
                        room = 4000 - len(text) - 30
                        if room > 200:
                            out = f"{text}\n\n{ui.ICO_NN} Разбор ИИ\n{ai_block[:room]}…"
                        else:
                            out = text[:3990] + "…"
            except Exception:
                logger.exception("session_group AI failed")
            try:
                await send_with_divert(
                    bot,
                    session,
                    intended_chat_id=group.chat_id,
                    intended_label=group.title or str(group.chat_id),
                    text=out,
                )
                await _mark_sent(session, group.chat_id, today, "recap")
            except Exception:
                logger.exception("evening recap failed chat=%s", group.chat_id)
                continue


async def maybe_send_live_recap(bot: Bot, session: AsyncSession, settings: Settings, day: date) -> None:
    _ = (bot, session, settings, day)
