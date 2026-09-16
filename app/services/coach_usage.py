"""Local LLM quota / usage tracking for admin (persisted in SQLite)."""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import desc, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.db.models import AppSetting, CoachUsageLog

logger = logging.getLogger("gymflex.coach_usage")

# Gemini free RPD resets at midnight Pacific Time
_PT = ZoneInfo("America/Los_Angeles")

SETTING_TOKENN_LAST_REMAIN = "tokenn_last_remain_quota"

LIVE_KIND = "live_set"
PROFILE_KINDS = frozenset({"week", "month", "exercise", "week_group", "week_plan"})
SESSION_KINDS = frozenset({"session", "session_group"})
# Fallback avg quota units when Tokenn samples are missing (from observed UI)
_FALLBACK_QUOTA = {
    "live_set": 14_000,
    "profile": 14_000,
    "other": 7_000,
}
_AVG_SAMPLE_LIMIT = 12


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


def kind_bucket(kind: str | None) -> str:
    k = (kind or "").strip() or "?"
    if k == LIVE_KIND:
        return "live_set"
    if k in PROFILE_KINDS:
        return "profile"
    if k in SESSION_KINDS:
        return "other"
    return "other"


_REQUEST_MAX = 48_000
_RESPONSE_MAX = 16_000


def _clip(text: str | None, limit: int) -> str | None:
    if not text:
        return None
    s = str(text)
    if len(s) <= limit:
        return s
    return s[: limit - 1] + "…"


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
    quota_cost: int = 0,
    request_text: str | None = None,
    response_text: str | None = None,
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
        quota_cost=int(quota_cost or 0),
        request_text=_clip(request_text, _REQUEST_MAX),
        response_text=_clip(response_text, _RESPONSE_MAX),
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


async def list_usage_logs(
    session: AsyncSession, *, offset: int = 0, limit: int = 10
) -> tuple[list[dict[str, Any]], int]:
    """Recent usage rows for admin list (newest first)."""
    total = (
        await session.execute(select(func.count()).select_from(CoachUsageLog))
    ).scalar_one()
    result = await session.execute(
        select(CoachUsageLog)
        .order_by(desc(CoachUsageLog.id))
        .offset(max(0, offset))
        .limit(max(1, min(limit, 20)))
    )
    rows = []
    for h in result.scalars().all():
        rows.append(
            {
                "id": h.id,
                "status": h.status,
                "provider": h.provider,
                "kind": h.kind,
                "user_label": h.user_label,
                "user_id": h.user_id,
                "duration_sec": round(float(h.duration_sec or 0), 1),
                "prompt_tokens": int(h.prompt_tokens or 0),
                "output_tokens": int(h.output_tokens or 0),
                "quota_cost": int(h.quota_cost or 0),
                "has_request": bool(h.request_text),
                "has_response": bool(h.response_text),
                "error": h.error,
                "finished_at": h.created_at.isoformat() if h.created_at else None,
            }
        )
    return rows, int(total or 0)


async def get_usage_log(
    session: AsyncSession, log_id: int
) -> dict[str, Any] | None:
    row = await session.get(CoachUsageLog, log_id)
    if row is None:
        return None
    return {
        "id": row.id,
        "status": row.status,
        "provider": row.provider,
        "kind": row.kind,
        "user_label": row.user_label,
        "user_id": row.user_id,
        "duration_sec": round(float(row.duration_sec or 0), 1),
        "prompt_tokens": int(row.prompt_tokens or 0),
        "output_tokens": int(row.output_tokens or 0),
        "quota_cost": int(row.quota_cost or 0),
        "request_text": row.request_text or "",
        "response_text": row.response_text or "",
        "error": row.error,
        "quota_id": row.quota_id,
        "quota_value": row.quota_value,
        "finished_at": row.created_at.isoformat() if row.created_at else None,
    }


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


def _avg_cost_for_bucket(
    rows: list[CoachUsageLog],
    bucket: str,
    *,
    prefer_quota_units: bool = False,
) -> dict[str, Any]:
    """Prefer quota_cost samples; else API tokens (unless prefer_quota_units); else fallback."""
    matched = [r for r in rows if kind_bucket(r.kind) == bucket and r.status == "ok"]
    with_cost = [r for r in matched if int(r.quota_cost or 0) > 0]
    if with_cost:
        samples = [int(r.quota_cost) for r in with_cost]
        avg = int(round(sum(samples) / len(samples)))
        return {
            "avg": avg,
            "n": len(samples),
            "unit": "quota",
            "fallback": False,
        }
    if matched and not prefer_quota_units:
        samples = [
            int(r.prompt_tokens or 0) + int(r.output_tokens or 0) for r in matched
        ]
        samples = [s for s in samples if s > 0]
        if samples:
            avg = int(round(sum(samples) / len(samples)))
            return {
                "avg": avg,
                "n": len(samples),
                "unit": "tokens",
                "fallback": False,
            }
    return {
        "avg": _FALLBACK_QUOTA[bucket],
        "n": 0,
        "unit": "quota",
        "fallback": True,
    }


async def _recent_ok_rows(session: AsyncSession) -> list[CoachUsageLog]:
    result = await session.execute(
        select(CoachUsageLog)
        .where(CoachUsageLog.status == "ok")
        .order_by(desc(CoachUsageLog.id))
        .limit(80)
    )
    return list(result.scalars().all())


def _remaining_actions(budget: int | None, avg: int, req_left: int) -> int | None:
    if budget is None:
        return req_left
    if avg <= 0:
        return req_left
    by_budget = budget // avg
    return min(by_budget, req_left)


async def measure_tokenn_quota_cost(session: AsyncSession) -> tuple[int, int | None]:
    """Fetch Tokenn remain_quota, return (cost_delta, new_remain)."""
    from app.services.llm_providers import PROVIDER_TOKENN, get_provider_spec
    from app.services.openai_llm import fetch_remain_quota

    spec = get_provider_spec(PROVIDER_TOKENN)
    if spec is None or not spec.api_key or not spec.base_url:
        return 0, None
    remain = await fetch_remain_quota(
        api_key=spec.api_key, base_url=spec.base_url, timeout=5.0
    )
    if remain is None:
        return 0, None

    cost = 0
    row = await session.get(AppSetting, SETTING_TOKENN_LAST_REMAIN)
    if row and (row.value or "").strip().isdigit():
        prev = int(row.value.strip())
        if prev >= remain:
            cost = prev - remain
    if row is None:
        session.add(AppSetting(key=SETTING_TOKENN_LAST_REMAIN, value=str(remain)))
    else:
        row.value = str(remain)
    try:
        await session.commit()
    except Exception:
        logger.exception("Failed to persist tokenn last remain")
        await session.rollback()
        return cost, remain
    return cost, remain


async def usage_snapshot(session: AsyncSession) -> dict[str, Any]:
    settings = get_settings()
    now = datetime.now(timezone.utc)
    day_start = pacific_day_start(now)
    minute_ago = now - timedelta(minutes=1)

    day_req, tok_in, tok_out, n429, by_kind = await _agg(session, day_start)
    rpd_left = max(0, int(settings.gemini_rpd_limit) - int(day_req))

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
                "quota_cost": int(h.quota_cost or 0),
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

    recent_ok = await _recent_ok_rows(session)
    # Prefer most recent samples per bucket (cap)
    by_bucket_rows: dict[str, list[CoachUsageLog]] = {
        "live_set": [],
        "profile": [],
        "other": [],
    }
    for r in recent_ok:
        b = kind_bucket(r.kind)
        if len(by_bucket_rows[b]) < _AVG_SAMPLE_LIMIT:
            by_bucket_rows[b].append(r)

    remain_quota: int | None = None
    try:
        from app.services.llm_providers import (
            PROVIDER_TOKENN,
            get_active_provider_id,
            get_provider_spec,
        )
        from app.services.openai_llm import fetch_remain_quota

        active = await get_active_provider_id(session)
        if active == PROVIDER_TOKENN:
            spec = get_provider_spec(PROVIDER_TOKENN)
            if spec and spec.api_key and spec.base_url:
                remain_quota = await fetch_remain_quota(
                    api_key=spec.api_key,
                    base_url=spec.base_url,
                    timeout=5.0,
                )
    except Exception:
        logger.debug("Tokenn balance in snapshot failed", exc_info=True)

    avgs = {
        b: _avg_cost_for_bucket(
            by_bucket_rows[b],
            b,
            prefer_quota_units=(remain_quota is not None),
        )
        for b in ("live_set", "profile", "other")
    }

    estimates: dict[str, Any] = {}
    for b, meta in avgs.items():
        left = _remaining_actions(remain_quota, int(meta["avg"]), rpd_left)
        estimates[b] = {
            **meta,
            "remaining": left,
            "rpd_left": rpd_left,
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
        "rpd_left": rpd_left,
        "remain_quota": remain_quota,
        "estimates": estimates,
        "day_start_pt": day_start.astimezone(_PT).isoformat(),
        "history": history,
        "last_429": last_429,
    }
