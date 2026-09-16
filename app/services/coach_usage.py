"""Local Gemini quota / usage tracking for admin (persisted in SQLite)."""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import desc, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.db.models import CoachUsageLog

logger = logging.getLogger("gymflex.coach_usage")

# Gemini free RPD resets at midnight Pacific Time
_PT = ZoneInfo("America/Los_Angeles")


def pacific_day_start(now: datetime | None = None) -> datetime:
    """UTC instant of today's Pacific midnight."""
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    local = now.astimezone(_PT)
    start_local = local.replace(hour=0, minute=0, second=0, microsecond=0)
    return start_local.astimezone(timezone.utc)


def _is_api_attempt(row: CoachUsageLog) -> bool:
    if row.quota_id == "soft_block":
        return False
    return row.status in {"ok", "429", "error"}


async def record_usage(
    session: AsyncSession,
    *,
    status: str,
    kind: str | None = None,
    provider: str | None = None,
    user_id: int | None = None,
    user_label: str | None = None,
    duration_sec: float = 0.0,
    prompt_tokens: int = 0,
    output_tokens: int = 0,
    error: str | None = None,
    quota_id: str | None = None,
    quota_value: str | None = None,
) -> None:
    row = CoachUsageLog(
        status=status,
        provider=(provider or "")[:32] or None,
        kind=kind,
        user_id=user_id,
        user_label=(user_label or "")[:64] or None,
        duration_sec=float(duration_sec or 0),
        prompt_tokens=int(prompt_tokens or 0),
        output_tokens=int(output_tokens or 0),
        error=(error or "")[:500] or None,
        quota_id=(quota_id or "")[:128] or None,
        quota_value=(quota_value or "")[:64] or None,
    )
    session.add(row)
    try:
        await session.commit()
    except Exception:
        logger.exception("Failed to persist coach usage")
        await session.rollback()


async def _agg(
    session: AsyncSession, since: datetime
) -> tuple[int, int, int, int, dict[str, int]]:
    """Returns api_req, tok_in, tok_out, n429, by_kind for rows since `since`."""
    result = await session.execute(
        select(CoachUsageLog).where(CoachUsageLog.created_at >= since)
    )
    rows = list(result.scalars().all())
    req = 0
    tok_in = 0
    tok_out = 0
    n429 = 0
    by_kind: dict[str, int] = {}
    for r in rows:
        if not _is_api_attempt(r):
            continue
        req += 1
        if r.status == "ok":
            tok_in += int(r.prompt_tokens or 0)
            tok_out += int(r.output_tokens or 0)
        if r.status == "429":
            n429 += 1
        k = r.kind or "?"
        by_kind[k] = by_kind.get(k, 0) + 1
    return req, tok_in, tok_out, n429, by_kind


async def is_quota_exhausted(session: AsyncSession) -> tuple[bool, str | None]:
    """Soft-block check against configured RPD/RPM limits."""
    settings = get_settings()
    now = datetime.now(timezone.utc)
    day_start = pacific_day_start(now)
    minute_ago = now - timedelta(minutes=1)

    day_req, _, _, _, _ = await _agg(session, day_start)
    if day_req >= settings.gemini_rpd_limit:
        return True, "rpd"

    result = await session.execute(
        select(CoachUsageLog).where(CoachUsageLog.created_at >= minute_ago)
    )
    rpm = sum(1 for r in result.scalars().all() if _is_api_attempt(r))
    if rpm >= settings.gemini_rpm_limit:
        return True, "rpm"
    return False, None


async def usage_snapshot(session: AsyncSession) -> dict[str, Any]:
    settings = get_settings()
    now = datetime.now(timezone.utc)
    day_start = pacific_day_start(now)
    minute_ago = now - timedelta(minutes=1)

    day_req, tok_in, tok_out, n429, by_kind = await _agg(session, day_start)

    result = await session.execute(
        select(CoachUsageLog).where(CoachUsageLog.created_at >= minute_ago)
    )
    rpm = sum(1 for r in result.scalars().all() if _is_api_attempt(r))

    hist = await session.execute(
        select(CoachUsageLog).order_by(desc(CoachUsageLog.id)).limit(15)
    )
    history = []
    for h in hist.scalars().all():
        history.append(
            {
                "status": h.status,
                "provider": h.provider,
                "kind": h.kind,
                "user_id": h.user_id,
                "user_label": h.user_label,
                "duration_sec": round(float(h.duration_sec or 0), 1),
                "prompt_tokens": h.prompt_tokens,
                "output_tokens": h.output_tokens,
                "error": h.error,
                "quota_id": h.quota_id,
                "quota_value": h.quota_value,
                "finished_at": h.created_at.isoformat() if h.created_at else None,
            }
        )

    last_429 = None
    r429 = await session.execute(
        select(CoachUsageLog)
        .where(CoachUsageLog.status == "429")
        .order_by(desc(CoachUsageLog.id))
        .limit(1)
    )
    row = r429.scalar_one_or_none()
    if row:
        last_429 = {
            "quota_id": row.quota_id,
            "quota_value": row.quota_value,
            "error": row.error,
            "at": row.created_at.isoformat() if row.created_at else None,
        }

    return {
        "provider": "gemini",
        "model": settings.gemini_model,
        "rpd_limit": settings.gemini_rpd_limit,
        "rpm_limit": settings.gemini_rpm_limit,
        "day_req": day_req,
        "day_tok_in": tok_in,
        "day_tok_out": tok_out,
        "day_429": n429,
        "by_kind": by_kind,
        "rpm": rpm,
        "day_start_pt": day_start.astimezone(_PT).isoformat(),
        "history": history,
        "last_429": last_429,
    }
