"""Ensure callback queries get answered so Telegram does not show a spinner forever."""

from __future__ import annotations

from typing import Any, Awaitable, Callable

from aiogram import BaseMiddleware
from aiogram.types import CallbackQuery, TelegramObject

from app.services.telegram_safe import safe_callback_answer


class EnsureCallbackAnsweredMiddleware(BaseMiddleware):
    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        try:
            return await handler(event, data)
        finally:
            if isinstance(event, CallbackQuery):
                await safe_callback_answer(event)
