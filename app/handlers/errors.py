from __future__ import annotations

import logging

from aiogram import Router
from aiogram.exceptions import TelegramBadRequest, TelegramNetworkError
from aiogram.types import ErrorEvent

from app.services.telegram_safe import is_benign_telegram_error, safe_callback_answer

router = Router(name="errors")
logger = logging.getLogger("gymflex.errors")


@router.error()
async def on_error(event: ErrorEvent) -> bool:
    exc = event.exception
    if is_benign_telegram_error(exc):
        # Stale callbacks, identical edits, transient Telegram network blips —
        # log quietly, do not spam the user with «ошибка в логах».
        logger.warning("Ignored benign Telegram error: %s", exc)
        return True

    logger.exception("Unhandled update error: %s", exc, exc_info=exc)
    update = event.update
    try:
        if update.callback_query:
            await safe_callback_answer(
                update.callback_query,
                "Не вышло, нажми ещё раз",
                show_alert=True,
            )
        elif update.message:
            await update.message.answer("Сбой на стороне бота. Попробуй ещё раз.")
    except (TelegramBadRequest, TelegramNetworkError):
        logger.debug("Could not notify user about error", exc_info=True)
    except Exception:
        logger.exception("Failed to notify user about error")
    return True
