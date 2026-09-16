"""Admin broadcast controls: test divert to DM + force-send digests."""

from __future__ import annotations

import html
import logging
from datetime import date, datetime
from typing import Any
from zoneinfo import ZoneInfo

from aiogram import Bot
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.db.models import AppSetting, User
from app.db.session import SessionLocal
from app import ui_copy as ui

logger = logging.getLogger("gymflex.broadcast_admin")

SETTING_TEST_DM = "broadcast_test_dm_telegram_id"

FORCE_KINDS = {
    "rem": "Утреннее напоминание",
    "rec": "Вечерняя сводка (факты)",
    "rai": "Вечерняя сводка + ИИ",
    "wg": "Неделя (общий чат / батч)",
    "wdm": "Неделя (мой личный разбор)",
}


async def get_test_dm_telegram_id(session: AsyncSession | None = None) -> int | None:
    async def _load(s: AsyncSession) -> int | None:
        row = await s.get(AppSetting, SETTING_TEST_DM)
        if not row or not (row.value or "").strip():
            return None
        raw = row.value.strip()
        if raw in {"0", "off", "false", "-"}:
            return None
        try:
            tid = int(raw)
        except ValueError:
            return None
        return tid if tid != 0 else None

    if session is not None:
        return await _load(session)
    async with SessionLocal() as s:
        return await _load(s)


async def set_test_dm_telegram_id(session: AsyncSession, telegram_id: int | None) -> None:
    value = str(int(telegram_id)) if telegram_id else "0"
    row = await session.get(AppSetting, SETTING_TEST_DM)
    if row is None:
        session.add(AppSetting(key=SETTING_TEST_DM, value=value))
    else:
        row.value = value
    await session.commit()


async def resolve_outbound(
    session: AsyncSession,
    *,
    intended_chat_id: int,
    intended_label: str | None = None,
) -> tuple[int, str]:
    """If test divert on → admin DM + prefix; else original chat."""
    test_id = await get_test_dm_telegram_id(session)
    if test_id is None:
        return intended_chat_id, ""
    label = (intended_label or str(intended_chat_id)).strip()
    prefix = f"[TEST · было бы → {label}]\n\n"
    return test_id, prefix


async def send_with_divert(
    bot: Bot,
    session: AsyncSession,
    *,
    intended_chat_id: int,
    intended_label: str | None,
    text: str,
    reply_markup: InlineKeyboardMarkup | None = None,
    parse_mode: str | None = None,
) -> int:
    """Send message honoring test divert. Returns actual chat_id used."""
    chat_id, prefix = await resolve_outbound(
        session,
        intended_chat_id=intended_chat_id,
        intended_label=intended_label,
    )
    body = f"{prefix}{text}" if prefix else text
    if len(body) > 4000:
        body = body[:3990] + "…"
    kwargs: dict[str, Any] = {}
    if reply_markup is not None:
        kwargs["reply_markup"] = reply_markup
    if parse_mode is not None:
        kwargs["parse_mode"] = parse_mode
    await bot.send_message(chat_id, body, **kwargs)
    return chat_id


async def build_morning_text(bot: Bot, session: AsyncSession, day: date) -> tuple[str, InlineKeyboardMarkup | None]:
    from app.services.reminders import WEEKDAY_NAMES, get_template_for_weekday

    template = await get_template_for_weekday(session, day.weekday())
    if not template:
        return "Сегодня нет дня в графике — напоминание пустое.", None
    exercise_lines = "\n".join(
        f"• {ex.name} ({ex.target_sets}×{ex.target_reps_min}-{ex.target_reps_max})"
        for ex in template.exercises
    )
    me = await bot.get_me()
    deep_link = f"https://t.me/{me.username}?start=workout"
    kb = InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text=ui.BTN_OPEN_BOT, url=deep_link)]]
    )
    text = (
        f"{ui.ICO_FIRE} Сегодня {WEEKDAY_NAMES[day.weekday()]} — {template.name}\n"
        f"#{template.hashtag}\n\n"
        f"{exercise_lines}\n\n"
        "Логируем в личке с ботом кнопками."
    )
    return text, kb


async def build_evening_fact(session: AsyncSession, day: date) -> tuple[str, Any]:
    from app.services.history import format_missing_today
    from app.services.recap import build_group_recap
    from app.services.reminders import get_template_for_weekday

    template = await get_template_for_weekday(session, day.weekday())
    if not template:
        return "Сегодня нет дня в графике — сводка пустая.", None
    text = await build_group_recap(session, template, day)
    missing = await format_missing_today(session, day, template.id)
    if "Все онборждённые" not in missing:
        text = f"{missing}\n\n{text}"
    return text, template


async def force_broadcast(
    bot: Bot,
    *,
    kind: str,
    target_chat_id: int,
    target_label: str,
    admin_user: User,
) -> str:
    """Force-send a digest to target_chat_id (no schedule/hour checks). Returns status text."""
    from sqlalchemy import select

    from app.services.coach_context import build_coach_context
    from app.services.nn_client import NnStatus, get_nn_status, request_coach
    from app.services.reminders import WEEKDAY_NAMES

    settings = get_settings()
    tz = ZoneInfo(settings.timezone)
    today = datetime.now(tz).date()

    async with SessionLocal() as session:
        if kind == "rem":
            text, kb = await build_morning_text(bot, session, today)
            await bot.send_message(target_chat_id, text, reply_markup=kb)
            return f"Отправлено: {FORCE_KINDS[kind]} → {target_label}"

        if kind == "rec":
            text, _ = await build_evening_fact(session, today)
            await bot.send_message(target_chat_id, text)
            return f"Отправлено: {FORCE_KINDS[kind]} → {target_label}"

        if kind == "rai":
            text, template = await build_evening_fact(session, today)
            if template is None:
                await bot.send_message(target_chat_id, text)
                return f"Отправлено (без ИИ, нет шаблона): → {target_label}"
            from app.services.ai_digests import (
                build_session_group_payload,
                wants_session_group,
            )

            ai_block = None
            if await get_nn_status() == NnStatus.online:
                users = list(
                    (
                        await session.execute(
                            select(User).where(User.onboarding_done.is_(True))
                        )
                    ).scalars().all()
                )
                athletes = [u for u in users if wants_session_group(u)]
                if athletes:
                    payload = await build_session_group_payload(
                        session,
                        day=today,
                        template=template,
                        athletes=athletes,
                        fact_recap=text,
                    )
                    ai_block = await request_coach(
                        kind="session_group",
                        athlete=payload,
                        focus={"date": today.isoformat(), "force": True},
                        history=[],
                        user_id=None,
                        user_label="force",
                    )
            out = text
            if ai_block:
                out = f"{text}\n\n{ui.ICO_NN} Разбор ИИ\n{ai_block.strip()}"
                if len(out) > 4000:
                    room = 4000 - len(text) - 30
                    out = (
                        f"{text}\n\n{ui.ICO_NN} Разбор ИИ\n{ai_block.strip()[:room]}…"
                        if room > 200
                        else text[:3990] + "…"
                    )
            elif await get_nn_status() != NnStatus.online:
                out = f"{text}\n\n(ИИ офлайн — только факты)"
            await bot.send_message(target_chat_id, out)
            return f"Отправлено: {FORCE_KINDS[kind]} → {target_label}"

        if kind == "wg":
            if await get_nn_status() != NnStatus.online:
                return "ИИ офлайн — week_group не отправлен"
            from app.services.ai_digests import build_week_group_payload, wants_week_group

            users = list(
                (
                    await session.execute(
                        select(User).where(User.onboarding_done.is_(True))
                    )
                ).scalars().all()
            )
            athletes = [u for u in users if wants_week_group(u)] or users
            if not athletes:
                return "Нет атлетов для недельного батча"
            payload = await build_week_group_payload(session, athletes=athletes)
            raw = await request_coach(
                kind="week_group",
                athlete=payload,
                focus={"date": today.isoformat(), "force": True},
                history=[],
                user_id=None,
                user_label="force",
            )
            if not raw:
                return "LLM не ответил на week_group"
            body = (
                f"{ui.ICO_NN} Недельный разбор команды "
                f"({WEEKDAY_NAMES[today.weekday()]})\n\n{ui.coach_html(raw)}"
            )
            if len(body) > 4000:
                body = body[:3990] + "…"
            await bot.send_message(target_chat_id, body)
            return f"Отправлено: {FORCE_KINDS[kind]} → {target_label}"

        if kind == "wdm":
            if await get_nn_status() != NnStatus.online:
                return "ИИ офлайн — личный week не отправлен"
            ctx = await build_coach_context(session, admin_user.id, kind="week")
            raw = await request_coach(
                kind="week",
                athlete=ctx,
                focus={"force": True},
                history=[],
                user_id=admin_user.id,
                user_label=admin_user.short_code,
            )
            if not raw:
                return "LLM не ответил на week"
            body = f"{ui.ICO_NN} <b>Недельный разбор</b>\n\n{ui.coach_html(raw)}"
            if len(body) > 4000:
                body = body[:3990] + "…"
            await bot.send_message(target_chat_id, body)
            return f"Отправлено: {FORCE_KINDS[kind]} → {target_label}"

    return f"Неизвестный kind={kind}"
