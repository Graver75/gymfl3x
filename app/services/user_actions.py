"""High-signal user action log for admin activity trail."""

from __future__ import annotations

import logging
from typing import Any

from sqlalchemy import desc, func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.db.models import User, UserActionLog
from app.db.session import SessionLocal

logger = logging.getLogger("gymflex.user_actions")

ACTION_LABELS: dict[str, str] = {
    "workout.start": "Старт тренировки",
    "workout.finish": "Финиш тренировки",
    "workout.skip": "Пропуск / отмена",
    "workout.reset": "Сброс тренировки",
    "workout.sets": "Запись подходов",
    "workout.note": "Заметка к упражнению",
    "profile.phase": "Смена фазы",
    "profile.log_level": "Уровень лога",
    "profile.sex": "Пол",
    "profile.weight": "Вес тела",
    "profile.name": "Имя",
    "profile.code": "Код",
    "profile.age": "Возраст",
    "profile.experience": "Стаж",
    "profile.ai": "Настройки ИИ",
    "profile.reset": "Обнуление прогресса",
    "history.delete": "Удаление сессии",
    "history.edit": "Правка подхода",
    "onboarding.done": "Онбординг завершён",
    "coach.live": "Совет по подходу",
    "coach.digest": "Разбор ИИ",
}


def action_label(action: str) -> str:
    return ACTION_LABELS.get(action, action)


async def log_action(
    user_id: int,
    action: str,
    *,
    detail: str | None = None,
    entity_type: str | None = None,
    entity_id: int | None = None,
) -> None:
    """Persist one action in its own session (never breaks caller txs)."""
    try:
        async with SessionLocal() as session:
            session.add(
                UserActionLog(
                    user_id=int(user_id),
                    action=(action or "")[:48],
                    detail=(detail or None) and str(detail)[:500],
                    entity_type=(entity_type or None) and str(entity_type)[:32],
                    entity_id=entity_id,
                )
            )
            await session.commit()
    except Exception:
        logger.exception("Failed to persist user action %s user=%s", action, user_id)


async def list_actions(
    session: AsyncSession,
    *,
    user_id: int | None = None,
    offset: int = 0,
    limit: int = 8,
) -> tuple[list[dict[str, Any]], int]:
    base = select(UserActionLog)
    count_q = select(func.count(UserActionLog.id))
    if user_id is not None:
        base = base.where(UserActionLog.user_id == user_id)
        count_q = count_q.where(UserActionLog.user_id == user_id)

    total = int((await session.execute(count_q)).scalar_one() or 0)
    rows = list(
        (
            await session.execute(
                base.options(selectinload(UserActionLog.user))
                .order_by(desc(UserActionLog.id))
                .offset(max(0, offset))
                .limit(max(1, limit))
            )
        )
        .scalars()
        .all()
    )
    out: list[dict[str, Any]] = []
    for row in rows:
        user: User | None = row.user
        out.append(
            {
                "id": row.id,
                "user_id": row.user_id,
                "action": row.action,
                "label": action_label(row.action),
                "detail": row.detail,
                "entity_type": row.entity_type,
                "entity_id": row.entity_id,
                "created_at": row.created_at,
                "code": user.short_code if user else None,
                "name": user.display_name if user else None,
            }
        )
    return out, total


async def get_action(session: AsyncSession, action_id: int) -> dict[str, Any] | None:
    row = (
        await session.execute(
            select(UserActionLog)
            .where(UserActionLog.id == action_id)
            .options(selectinload(UserActionLog.user))
        )
    ).scalar_one_or_none()
    if not row:
        return None
    user = row.user
    return {
        "id": row.id,
        "user_id": row.user_id,
        "action": row.action,
        "label": action_label(row.action),
        "detail": row.detail,
        "entity_type": row.entity_type,
        "entity_id": row.entity_id,
        "created_at": row.created_at,
        "code": user.short_code if user else None,
        "name": user.display_name if user else None,
    }
