"""Clear FSM when user presses a main reply-menu button."""

from __future__ import annotations

from typing import Any, Awaitable, Callable

from aiogram import BaseMiddleware
from aiogram.fsm.context import FSMContext
from aiogram.types import Message, TelegramObject

from app import ui_copy as ui

MAIN_MENU_TEXTS = frozenset(
    {
        ui.BTN_WORKOUT,
        ui.BTN_TODAY,
        ui.BTN_HISTORY,
        ui.BTN_PROGRAM,
        ui.BTN_PROFILE,
        ui.BTN_COACH,
        ui.BTN_ADMIN,
    }
)


class ClearStateOnMenuMiddleware(BaseMiddleware):
    """Menu buttons always win over a stuck mid-flow FSM state."""

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        if isinstance(event, Message) and (event.text or "") in MAIN_MENU_TEXTS:
            state: FSMContext | None = data.get("state")
            if state is not None:
                await state.clear()
        return await handler(event, data)
