"""Scheduled / batched AI digests (session_group, week personal+group)."""

from __future__ import annotations

import html
import logging
from datetime import date, datetime
from typing import Any
from zoneinfo import ZoneInfo

from aiogram import Bot
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.config import Settings, get_settings
from app.db.models import (
    GroupChat,
    SessionStatus,
    User,
    WorkoutSession,
    WorkoutTemplate,
)
from app.db.session import SessionLocal
from app.services.coach_context import build_coach_context
from app.services.nn_client import NnStatus, get_nn_status, request_coach
from app.services.reminders import (
    WEEKDAY_NAMES,
    _already_sent,
    _mark_sent,
)
from app import ui_copy as ui

logger = logging.getLogger("gymflex.ai_digests")


def _dest(user: User) -> str:
    d = (getattr(user, "ai_dest", None) or "both").strip().lower()
    return d if d in {"dm", "group", "both"} else "both"


def wants_session_dm(user: User) -> bool:
    return bool(getattr(user, "ai_session_enabled", True)) and _dest(user) in {
        "dm",
        "both",
    }


def wants_session_group(user: User) -> bool:
    return bool(getattr(user, "ai_session_enabled", True)) and _dest(user) in {
        "group",
        "both",
    }


def wants_week_dm(user: User) -> bool:
    return bool(getattr(user, "ai_week_enabled", True)) and _dest(user) in {
        "dm",
        "both",
    }


def wants_week_group(user: User) -> bool:
    return bool(getattr(user, "ai_week_enabled", True)) and _dest(user) in {
        "group",
        "both",
    }


async def _finished_today(
    session: AsyncSession, day: date, template_id: int
) -> list[WorkoutSession]:
    result = await session.execute(
        select(WorkoutSession)
        .where(
            WorkoutSession.session_date == day,
            WorkoutSession.template_id == template_id,
            WorkoutSession.status == SessionStatus.finished,
        )
        .options(
            selectinload(WorkoutSession.user),
            selectinload(WorkoutSession.sets),
        )
    )
    return list(result.scalars().all())


async def build_session_group_payload(
    session: AsyncSession,
    *,
    day: date,
    template: WorkoutTemplate,
    athletes: list[User],
    fact_recap: str,
) -> dict[str, Any]:
    """Multi-athlete JSON for session_group LLM call."""
    finished = await _finished_today(session, day, template.id)
    by_user = {ws.user_id: ws for ws in finished}
    rows: list[dict[str, Any]] = []
    for user in athletes:
        ws = by_user.get(user.id)
        ctx = await build_coach_context(
            session,
            user.id,
            kind="session",
            focus_session_id=ws.id if ws else None,
        )
        rows.append(
            {
                "code": user.short_code,
                "name": user.display_name,
                "user": ctx.get("user"),
                "sessions": ctx.get("sessions"),
                "exercise_state": ctx.get("exercise_state"),
                "focus": ctx.get("focus"),
            }
        )
    return {
        "kind": "session_group",
        "date": day.isoformat(),
        "template": template.name,
        "hashtag": template.hashtag,
        "fact_recap": fact_recap[:3500],
        "athletes": rows,
    }


async def build_week_group_payload(
    session: AsyncSession, *, athletes: list[User]
) -> dict[str, Any]:
    rows: list[dict[str, Any]] = []
    for user in athletes:
        ctx = await build_coach_context(session, user.id, kind="week")
        rows.append(
            {
                "code": user.short_code,
                "name": user.display_name,
                "user": ctx.get("user"),
                "adherence": ctx.get("adherence"),
                "aggregates": ctx.get("aggregates"),
                "sessions": ctx.get("sessions"),
                "exercise_state": ctx.get("exercise_state"),
            }
        )
    return {"kind": "week_group", "athletes": rows}


async def append_session_group_ai(
    bot: Bot,
    session: AsyncSession,
    *,
    group: GroupChat,
    day: date,
    template: WorkoutTemplate,
    fact_text: str,
) -> str | None:
    """Generate Bender group block; returns text or None."""
    if await get_nn_status() != NnStatus.online:
        return None
    finished = await _finished_today(session, day, template.id)
    candidates = [
        ws.user
        for ws in finished
        if ws.user and wants_session_group(ws.user) and ws.user.onboarding_done
    ]
    # unique by id
    seen: set[int] = set()
    athletes: list[User] = []
    for u in candidates:
        if u.id not in seen:
            seen.add(u.id)
            athletes.append(u)
    if not athletes:
        return None
    if await _already_sent(session, group.chat_id, day, "ai_session_group"):
        return None
    payload = await build_session_group_payload(
        session,
        day=day,
        template=template,
        athletes=athletes,
        fact_recap=fact_text,
    )
    raw = await request_coach(
        kind="session_group",
        athlete=payload,
        focus={"date": day.isoformat(), "template_id": template.id},
        history=[],
        user_id=None,
        user_label="group",
    )
    if not raw:
        return None
    await _mark_sent(session, group.chat_id, day, "ai_session_group")
    return raw.strip()


async def send_week_digests(bot: Bot, settings: Settings) -> None:
    """Sunday (or configured weekday) personal + one group batch."""
    tz = ZoneInfo(settings.timezone)
    now = datetime.now(tz)
    today = now.date()

    async with SessionLocal() as session:
        groups = list(
            (
                await session.execute(select(GroupChat).where(GroupChat.active.is_(True)))
            ).scalars().all()
        )
        # Determine if this hour matches any group's week digest schedule
        active_groups: list[GroupChat] = []
        for g in groups:
            wh = (
                g.week_digest_hour
                if g.week_digest_hour is not None
                else settings.week_digest_hour
            )
            wd = (
                g.week_digest_weekday
                if g.week_digest_weekday is not None
                else settings.week_digest_weekday
            )
            if now.hour == wh and today.weekday() == wd:
                active_groups.append(g)

        if not active_groups and not (
            now.hour == settings.week_digest_hour
            and today.weekday() == settings.week_digest_weekday
        ):
            return

        if await get_nn_status() != NnStatus.online:
            return

        # Hidden week_plan job (same schedule window; own dedup)
        try:
            from app.services.week_plan import maybe_run_scheduled_week_plan

            await maybe_run_scheduled_week_plan(bot)
        except Exception:
            logger.exception("scheduled week_plan failed")

        try:
            from app.services.program_review import maybe_run_scheduled_program_review

            await maybe_run_scheduled_program_review(bot)
        except Exception:
            logger.exception("scheduled program_review failed")

        users = list(
            (
                await session.execute(
                    select(User).where(User.onboarding_done.is_(True))
                )
            ).scalars().all()
        )

        # Personal DMs (dedup per user via RecapSent chat_id=telegram_id)
        for user in users:
            if not wants_week_dm(user):
                continue
            if await _already_sent(session, user.telegram_id, today, "ai_week"):
                continue
            ctx = await build_coach_context(session, user.id, kind="week")
            raw = await request_coach(
                kind="week",
                athlete=ctx,
                focus={},
                history=[],
                user_id=user.id,
                user_label=user.short_code,
            )
            if not raw:
                continue
            try:
                body = f"{ui.ICO_NN} <b>Недельный разбор</b>\n\n{ui.coach_html(raw)}"
                if len(body) > 4000:
                    body = body[:3990] + "…"
                from app.services.broadcast_admin import send_with_divert

                await send_with_divert(
                    bot,
                    session,
                    intended_chat_id=user.telegram_id,
                    intended_label=f"DM {user.short_code}",
                    text=body,
                    parse_mode="HTML",
                )
                await _mark_sent(session, user.telegram_id, today, "ai_week")
            except Exception:
                logger.exception("week DM failed user=%s", user.id)

        # Group batch — one message per active group
        group_athletes = [u for u in users if wants_week_group(u)]
        if not group_athletes:
            return
        targets = active_groups or groups
        for group in targets:
            wh = (
                group.week_digest_hour
                if group.week_digest_hour is not None
                else settings.week_digest_hour
            )
            wd = (
                group.week_digest_weekday
                if group.week_digest_weekday is not None
                else settings.week_digest_weekday
            )
            if now.hour != wh or today.weekday() != wd:
                continue
            if await _already_sent(session, group.chat_id, today, "ai_week_group"):
                continue
            payload = await build_week_group_payload(session, athletes=group_athletes)
            raw = await request_coach(
                kind="week_group",
                athlete=payload,
                focus={"date": today.isoformat()},
                history=[],
                user_id=None,
                user_label="group",
            )
            if not raw:
                continue
            try:
                body = (
                    f"{ui.ICO_NN} Недельный разбор команды "
                    f"({WEEKDAY_NAMES[today.weekday()]})\n\n{ui.coach_html(raw)}"
                )
                if len(body) > 4000:
                    body = body[:3990] + "…"
                from app.services.broadcast_admin import send_with_divert

                await send_with_divert(
                    bot,
                    session,
                    intended_chat_id=group.chat_id,
                    intended_label=group.title or str(group.chat_id),
                    text=body,
                )
                await _mark_sent(session, group.chat_id, today, "ai_week_group")
            except Exception:
                logger.exception("week group failed chat=%s", group.chat_id)


def digest_schedule_snapshot(settings: Settings | None = None) -> dict[str, Any]:
    s = settings or get_settings()
    return {
        "recap_hour": s.recap_hour,
        "week_digest_hour": s.week_digest_hour,
        "week_digest_weekday": s.week_digest_weekday,
        "week_digest_weekday_name": WEEKDAY_NAMES[s.week_digest_weekday % 7],
        "kinds": [
            "session",
            "session_group",
            "week",
            "week_group",
            "week_plan",
            "live_set",
        ],
        "coming_soon": [
            "morning_hype",
            "skip_roast",
            "month_digest",
            "pr_alerts",
        ],
    }
