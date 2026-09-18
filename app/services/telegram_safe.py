"""Helpers for Telegram API calls that often fail harmlessly."""

from __future__ import annotations

import logging
from typing import Any

from aiogram.exceptions import TelegramBadRequest, TelegramNetworkError
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, Message

logger = logging.getLogger("gymflex.tg")

_BENIGN_BAD_REQUEST = (
    "query is too old",
    "query id is invalid",
    "message is not modified",
    "message to edit not found",
    "message can't be edited",
    "button_data_invalid",
)


def is_benign_telegram_error(exc: BaseException) -> bool:
    text = str(exc).lower()
    if isinstance(exc, TelegramNetworkError):
        return True
    if isinstance(exc, TelegramBadRequest):
        return any(s in text for s in _BENIGN_BAD_REQUEST)
    return False


def chunk_telegram_text(text: str, limit: int = 3500) -> list[str]:
    """Split long Telegram HTML/text on newlines; never mid-word hard-cut."""
    text = (text or "").rstrip()
    if not text:
        return []
    if len(text) <= limit:
        return [text]
    chunks: list[str] = []
    buf: list[str] = []
    size = 0
    for line in text.split("\n"):
        # Hard-split absurdly long single lines (no newlines)
        while len(line) > limit:
            if buf:
                chunks.append("\n".join(buf))
                buf = []
                size = 0
            chunks.append(line[:limit])
            line = line[limit:]
        add = len(line) + (1 if buf else 0)
        if buf and size + add > limit:
            chunks.append("\n".join(buf))
            buf = [line]
            size = len(line)
        else:
            buf.append(line)
            size += add
    if buf:
        chunks.append("\n".join(buf))
    return chunks


async def safe_callback_answer(
    callback: CallbackQuery,
    text: str | None = None,
    *,
    show_alert: bool = False,
) -> None:
    try:
        await callback.answer(text, show_alert=show_alert)
    except Exception as exc:
        if is_benign_telegram_error(exc):
            logger.debug("callback.answer skipped: %s", exc)
            return
        logger.warning("callback.answer failed: %s", exc)


async def safe_edit_text(
    message: Message,
    text: str,
    *,
    reply_markup: InlineKeyboardMarkup | None = None,
    **kwargs: Any,
) -> bool:
    """edit_text; ignore 'message is not modified'. Returns False if skipped/failed benignly."""
    try:
        await message.edit_text(text, reply_markup=reply_markup, **kwargs)
        return True
    except TelegramBadRequest as exc:
        if "message is not modified" in str(exc).lower():
            return False
        if is_benign_telegram_error(exc):
            logger.debug("edit_text skipped: %s", exc)
            return False
        raise
