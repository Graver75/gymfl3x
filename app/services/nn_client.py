"""Coach status / request façade — multi-provider remote LLM."""

from __future__ import annotations

import asyncio
import json
import logging
import time
from enum import Enum
from typing import Any

from app.config import get_settings
from app.db.session import SessionLocal
from app.services.coach_prompts import DATA_SCHEMA_RU, SYSTEM_PROMPT, user_prompt_for
from app.services.coach_usage import is_quota_exhausted, record_usage, usage_snapshot
from app.services.llm_providers import (
    generate_with_provider,
    get_active_provider_id,
    get_provider_spec,
    is_coach_enabled,
    provider_configured,
    providers_status_snapshot,
)

logger = logging.getLogger("gymflex.nn")

_STATUS_TTL_SEC = 45.0
_status_cache: tuple[float, "NnStatus"] | None = None
_status_lock = asyncio.Lock()
_meta_cache: tuple[float, dict[str, Any]] | None = None
_META_TTL_SEC = 300.0

_live_cooldown: dict[int, float] = {}


class NnStatus(str, Enum):
    disabled = "disabled"
    offline = "offline"
    online = "online"


STATUS_LABELS = {
    NnStatus.disabled: "выключена",
    NnStatus.offline: "офлайн",
    NnStatus.online: "онлайн",
}


def status_label(status: NnStatus) -> str:
    return STATUS_LABELS.get(status, status.value)


def invalidate_status_cache() -> None:
    global _status_cache
    _status_cache = None


def live_cooldown_remaining(user_id: int) -> float:
    deadline = _live_cooldown.get(user_id)
    if not deadline:
        return 0.0
    left = deadline - time.monotonic()
    if left <= 0:
        _live_cooldown.pop(user_id, None)
        return 0.0
    return left


def mark_live_cooldown(user_id: int) -> None:
    settings = get_settings()
    _live_cooldown[user_id] = time.monotonic() + float(
        settings.coach_live_cooldown_sec or 90
    )


async def get_nn_status(*, force: bool = False) -> NnStatus:
    """Never raises. Uses a short TTL cache."""
    global _status_cache
    settings = get_settings()
    if not settings.nn_enabled:
        return NnStatus.disabled
    try:
        if not await is_coach_enabled():
            return NnStatus.disabled
    except Exception:
        logger.debug("coach_enabled check failed", exc_info=True)

    now = time.monotonic()
    if not force and _status_cache and now - _status_cache[0] < _STATUS_TTL_SEC:
        return _status_cache[1]

    async with _status_lock:
        now = time.monotonic()
        if not force and _status_cache and now - _status_cache[0] < _STATUS_TTL_SEC:
            return _status_cache[1]
        # Re-check toggle inside lock (admin may have flipped it)
        try:
            if not await is_coach_enabled():
                status = NnStatus.disabled
                _status_cache = (time.monotonic(), status)
                return status
        except Exception:
            pass
        status = await _resolve_status()
        _status_cache = (time.monotonic(), status)
        return status


async def _resolve_status() -> NnStatus:
    try:
        async with SessionLocal() as session:
            exhausted, reason = await is_quota_exhausted(session)
            if exhausted:
                logger.info("Coach soft-blocked (%s)", reason)
                return NnStatus.offline
            pid = await get_active_provider_id(session)
    except Exception:
        logger.debug("Quota/provider resolve failed", exc_info=True)
        pid = await get_active_provider_id()

    spec = get_provider_spec(pid)
    if spec is None or not provider_configured(spec):
        return NnStatus.offline

    from app.services.llm_providers import ping_provider

    try:
        ok = await ping_provider(spec)
    except Exception:
        ok = False
    return NnStatus.online if ok else NnStatus.offline


async def fetch_nn_meta(*, force: bool = False) -> dict[str, Any]:
    global _meta_cache
    now = time.monotonic()
    if not force and _meta_cache and now - _meta_cache[0] < _META_TTL_SEC:
        return _meta_cache[1]

    pid = await get_active_provider_id()
    spec = get_provider_spec(pid)
    data = {
        "system_prompt": SYSTEM_PROMPT,
        "data_schema_ru": DATA_SCHEMA_RU,
        "model": spec.model if spec else "?",
        "provider": pid,
        "max_history": 12,
    }
    _meta_cache = (time.monotonic(), data)
    return data


async def fetch_nn_load() -> dict[str, Any] | None:
    """Admin usage + provider status. Never raises."""
    settings = get_settings()
    try:
        async with SessionLocal() as session:
            snap = await usage_snapshot(session)
            active = await get_active_provider_id(session)
            manual_on = await is_coach_enabled(session)
        providers = await providers_status_snapshot()
        status = await get_nn_status(force=True)
        active_spec = get_provider_spec(active)
        snap["status"] = status.value
        snap["provider"] = active
        snap["provider_label"] = active_spec.label if active_spec else active
        snap["model"] = active_spec.model if active_spec else settings.gemini_model
        snap["key_configured"] = bool(
            active_spec and provider_configured(active_spec)
        )
        snap["providers"] = providers
        snap["coach_enabled"] = manual_on
        snap["env_nn_enabled"] = bool(settings.nn_enabled)
        return snap
    except Exception as exc:
        logger.warning("Coach usage snapshot failed: %s", exc)
        return {
            "provider": "?",
            "model": settings.gemini_model,
            "status": "error",
            "error": str(exc)[:200],
            "key_configured": False,
            "day_req": 0,
            "day_tok_in": 0,
            "day_tok_out": 0,
            "day_429": 0,
            "by_kind": {},
            "rpm": 0,
            "rpd_limit": settings.gemini_rpd_limit,
            "rpm_limit": settings.gemini_rpm_limit,
            "history": [],
            "last_429": None,
            "providers": [],
            "coach_enabled": False,
            "env_nn_enabled": bool(settings.nn_enabled),
        }


async def request_coach(
    *,
    kind: str,
    athlete: dict[str, Any],
    focus: dict[str, Any] | None = None,
    history: list[dict[str, str]] | None = None,
    user_id: int | None = None,
    user_label: str | None = None,
    locale: str = "ru",
) -> str | None:
    """Generate coach text via active LLM provider. Never raises."""
    settings = get_settings()
    if not settings.nn_enabled:
        return None
    try:
        if not await is_coach_enabled():
            return None
    except Exception:
        return None

    pid = await get_active_provider_id()
    spec = get_provider_spec(pid)
    if spec is None or not provider_configured(spec):
        return None

    try:
        async with SessionLocal() as session:
            exhausted, reason = await is_quota_exhausted(session)
            if exhausted:
                await record_usage(
                    session,
                    status="error",
                    kind=kind,
                    provider=pid,
                    user_id=user_id,
                    user_label=user_label,
                    error=f"soft_block:{reason}",
                    quota_id="soft_block",
                    quota_value=reason,
                )
                invalidate_status_cache()
                return None
    except Exception:
        logger.debug("Pre-flight quota check failed", exc_info=True)

    try:
        athlete_json = json.dumps(athlete, ensure_ascii=False, default=str)
    except Exception:
        athlete_json = str(athlete)
    if len(athlete_json) > 28_000:
        athlete_json = athlete_json[:27_990] + "…"

    user_text = user_prompt_for(kind, athlete_json, focus or {}, locale=locale)

    hist = history or []
    if kind == "session":
        hist = []
    elif kind == "live_set":
        hist = hist[-2:]
    else:
        hist = hist[-6:]

    used_pid, result = await generate_with_provider(
        provider_id=pid,
        system=SYSTEM_PROMPT,
        user_text=user_text,
        history=hist,
    )

    req_payload = user_text
    if hist:
        bits = []
        for turn in hist:
            role = (turn.get("role") or "user")[:9]
            content = (turn.get("content") or "").strip()
            if content:
                bits.append(f"[{role}] {content}")
        if bits:
            req_payload = (
                "--- dialog ---\n"
                + "\n".join(bits)
                + "\n--- prompt ---\n"
                + user_text
            )
    resp_payload = (result.text or "").strip() if result.status == "ok" else None
    if result.status != "ok" and result.error:
        resp_payload = f"[error] {result.error}"

    try:
        async with SessionLocal() as session:
            quota_cost = 0
            if used_pid == "tokenn" and result.status == "ok":
                try:
                    from app.services.coach_usage import measure_tokenn_quota_cost

                    quota_cost, _ = await measure_tokenn_quota_cost(session)
                except Exception:
                    logger.debug("Tokenn quota cost measure failed", exc_info=True)
            await record_usage(
                session,
                status=result.status,
                kind=kind,
                provider=used_pid,
                user_id=user_id,
                user_label=user_label,
                duration_sec=result.duration_sec,
                prompt_tokens=result.prompt_tokens,
                output_tokens=result.output_tokens,
                quota_cost=quota_cost,
                request_text=req_payload,
                response_text=resp_payload,
                error=result.error,
                quota_id=result.quota_id,
                quota_value=result.quota_value,
            )
    except Exception:
        logger.exception("Usage log failed")

    if result.status != "ok":
        invalidate_status_cache()
        return None

    if kind == "live_set" and user_id is not None:
        mark_live_cooldown(user_id)

    return (result.text or "").strip() or None
