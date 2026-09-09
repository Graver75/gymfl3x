from __future__ import annotations

import logging

from aiogram import Router
from aiogram.types import ErrorEvent

router = Router(name="errors")
logger = logging.getLogger("gymflex.errors")


@router.error()
async def on_error(event: ErrorEvent) -> bool:
    logger.exception(
        "Unhandled update error: %s",
        event.exception,
        exc_info=event.exception,
    )
    update = event.update
    try:
        if update.message:
            await update.message.answer("Сбой на стороне бота, уже в логах. Попробуй ещё раз.")
        elif update.callback_query:
            await update.callback_query.answer("Ошибка, попробуй ещё раз", show_alert=True)
    except Exception:
        logger.exception("Failed to notify user about error")
    return True
