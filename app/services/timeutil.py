"""UTC time helpers for consistent session timestamps."""

from __future__ import annotations

from datetime import datetime, timezone

from zoneinfo import ZoneInfo

from app.config import get_settings


def utc_now() -> datetime:
    """Naive UTC now for SQLite DateTime columns (convention: store UTC)."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


def as_utc(dt: datetime | None) -> datetime | None:
    """Normalize to UTC-aware. Naive values are treated as already-UTC."""
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def as_local(dt: datetime | None) -> datetime | None:
    """Convert to app timezone (Europe/Moscow by default) for display."""
    if dt is None:
        return None
    settings = get_settings()
    try:
        tz = ZoneInfo(settings.timezone)
    except Exception:
        tz = timezone.utc
    return as_utc(dt).astimezone(tz)


def duration_seconds(started: datetime | None, finished: datetime | None) -> int | None:
    a = as_utc(started)
    b = as_utc(finished)
    if a is None or b is None:
        return None
    total = int((b - a).total_seconds())
    if total < 0:
        return None
    return total
