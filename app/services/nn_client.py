"""Fault-tolerant client for the optional gymflex-nn service."""

from __future__ import annotations

import asyncio
import logging
import time
from enum import Enum
from typing import Any

import httpx

from app.config import get_settings

logger = logging.getLogger("gymflex.nn")

_STATUS_TTL_SEC = 45.0
_status_cache: tuple[float, "NnStatus"] | None = None
_status_lock = asyncio.Lock()
_meta_cache: tuple[float, dict[str, Any]] | None = None
_META_TTL_SEC = 300.0


class NnStatus(str, Enum):
    disabled = "disabled"
    offline = "offline"
    online = "online"


STATUS_LABELS = {
    NnStatus.disabled: "выключена",
    NnStatus.offline: "офлайн",
    NnStatus.online: "онлайн",
}

# Fallback if /v1/meta unavailable
_FALLBACK_META = {
    "system_prompt": (
        "Ты — краткий русскоязычный коуч по силовым тренировкам в зале.\n"
        "Опирайся только на JSON-данные атлета…"
    ),
    "data_schema_ru": (
        "В запрос уходит компактный JSON только вашего атлета: профиль, adherence, "
        "сессии, подходы, заметки, вес тела, focus текущего разбора."
    ),
    "model": "unknown",
    "max_history": 12,
}


def status_label(status: NnStatus) -> str:
    return STATUS_LABELS.get(status, status.value)


def invalidate_status_cache() -> None:
    global _status_cache
    _status_cache = None


async def get_nn_status(*, force: bool = False) -> NnStatus:
    """Never raises. Uses a short TTL cache."""
    global _status_cache
    settings = get_settings()
    if not settings.nn_enabled:
        return NnStatus.disabled

    now = time.monotonic()
    if not force and _status_cache and now - _status_cache[0] < _STATUS_TTL_SEC:
        return _status_cache[1]

    async with _status_lock:
        now = time.monotonic()
        if not force and _status_cache and now - _status_cache[0] < _STATUS_TTL_SEC:
            return _status_cache[1]
        status = await _ping_health()
        _status_cache = (time.monotonic(), status)
        return status


async def _ping_health() -> NnStatus:
    settings = get_settings()
    url = f"{settings.nn_url.rstrip('/')}/health"
    try:
        async with httpx.AsyncClient(timeout=settings.nn_health_timeout_sec) as client:
            r = await client.get(url)
            if r.status_code == 200:
                data = r.json()
                if isinstance(data, dict) and data.get("ok"):
                    return NnStatus.online
            return NnStatus.offline
    except Exception as exc:
        logger.debug("NN health offline: %s", exc)
        return NnStatus.offline


async def fetch_nn_meta(*, force: bool = False) -> dict[str, Any]:
    """System prompt + data schema. Never raises."""
    global _meta_cache
    now = time.monotonic()
    if not force and _meta_cache and now - _meta_cache[0] < _META_TTL_SEC:
        return _meta_cache[1]

    settings = get_settings()
    if not settings.nn_enabled:
        return dict(_FALLBACK_META)

    url = f"{settings.nn_url.rstrip('/')}/v1/meta"
    try:
        async with httpx.AsyncClient(timeout=settings.nn_health_timeout_sec) as client:
            r = await client.get(url)
            if r.status_code == 200:
                data = r.json()
                if isinstance(data, dict) and data.get("system_prompt"):
                    _meta_cache = (time.monotonic(), data)
                    return data
    except Exception as exc:
        logger.debug("NN meta failed: %s", exc)
    return dict(_FALLBACK_META)


async def request_coach(
    *,
    kind: str,
    athlete: dict[str, Any],
    focus: dict[str, Any] | None = None,
    history: list[dict[str, str]] | None = None,
    user_id: int | None = None,
    locale: str = "ru",
) -> str | None:
    """Call POST /v1/coach. Returns text or None. Never raises."""
    settings = get_settings()
    if not settings.nn_enabled:
        return None
    status = await get_nn_status()
    if status != NnStatus.online:
        return None

    url = f"{settings.nn_url.rstrip('/')}/v1/coach"
    payload = {
        "kind": kind,
        "locale": locale,
        "user_id": user_id,
        "athlete": athlete,
        "focus": focus or {},
        "history": history or [],
    }
    try:
        async with httpx.AsyncClient(timeout=settings.nn_timeout_sec) as client:
            r = await client.post(url, json=payload)
            if r.status_code != 200:
                logger.warning("NN coach HTTP %s: %s", r.status_code, r.text[:200])
                invalidate_status_cache()
                return None
            data = r.json()
            text = (data.get("text") or "").strip()
            return text or None
    except Exception as exc:
        logger.warning("NN coach failed: %s", exc)
        invalidate_status_cache()
        return None
