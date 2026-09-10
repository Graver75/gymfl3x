"""Per-user NN coach dialogue history (SQLite)."""

from __future__ import annotations

from typing import Any

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import NnDialogMessage

# Keep context small for CPU models
MAX_HISTORY_MESSAGES = 12
MAX_STORED_CONTENT = 8000


async def dialog_turn_count(session: AsyncSession, user_id: int) -> int:
    result = await session.execute(
        select(func.count()).select_from(NnDialogMessage).where(NnDialogMessage.user_id == user_id)
    )
    return int(result.scalar_one() or 0)


async def load_dialog_history(
    session: AsyncSession,
    user_id: int,
    *,
    limit: int = MAX_HISTORY_MESSAGES,
) -> list[dict[str, str]]:
    result = await session.execute(
        select(NnDialogMessage)
        .where(NnDialogMessage.user_id == user_id)
        .order_by(NnDialogMessage.id.desc())
        .limit(limit)
    )
    rows = list(reversed(result.scalars().all()))
    return [{"role": r.role, "content": r.content} for r in rows]


async def append_dialog_turn(
    session: AsyncSession,
    user_id: int,
    *,
    role: str,
    content: str,
    kind: str | None = None,
) -> None:
    text = content.strip()
    if len(text) > MAX_STORED_CONTENT:
        text = text[: MAX_STORED_CONTENT - 1] + "…"
    session.add(
        NnDialogMessage(user_id=user_id, role=role, content=text, kind=kind)
    )
    await session.flush()


async def clear_dialog(session: AsyncSession, user_id: int) -> int:
    result = await session.execute(
        delete(NnDialogMessage).where(NnDialogMessage.user_id == user_id)
    )
    await session.commit()
    return int(result.rowcount or 0)


def history_for_api(messages: list[dict[str, str]]) -> list[dict[str, Any]]:
    return [{"role": m["role"], "content": m["content"]} for m in messages]
