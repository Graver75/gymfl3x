"""One-shot fix: finished_at was stored as Moscow wall clock, started_at as UTC."""

from __future__ import annotations

import logging
from datetime import timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from zoneinfo import ZoneInfo

from app.config import get_settings
from app.db.models import AppSetting, WorkoutSession

logger = logging.getLogger("gymflex.time")

SETTING_FLAG = "session_finished_at_utc_v1"


async def migrate_session_finished_at_to_utc(session: AsyncSession) -> int:
    """Convert naive finished_at from app timezone → UTC. Returns rows updated."""
    row = await session.get(AppSetting, SETTING_FLAG)
    if row is not None:
        return 0

    settings = get_settings()
    try:
        local_tz = ZoneInfo(settings.timezone)
    except Exception:
        local_tz = timezone.utc

    sessions = list(
        (
            await session.execute(
                select(WorkoutSession).where(WorkoutSession.finished_at.is_not(None))
            )
        )
        .scalars()
        .all()
    )
    updated = 0
    for ws in sessions:
        fi = ws.finished_at
        if fi is None:
            continue
        if fi.tzinfo is not None:
            new = fi.astimezone(timezone.utc).replace(tzinfo=None)
        else:
            # Bug: finish wrote local wall clock as naive; treat as settings.timezone
            new = fi.replace(tzinfo=local_tz).astimezone(timezone.utc).replace(tzinfo=None)
        old = fi.replace(tzinfo=None) if fi.tzinfo is not None else fi
        if new != old:
            ws.finished_at = new
            updated += 1

    session.add(AppSetting(key=SETTING_FLAG, value="1"))
    await session.commit()
    logger.info(
        "Migrated session finished_at to UTC: %s/%s rows (from %s)",
        updated,
        len(sessions),
        settings.timezone,
    )
    return updated
