from aiogram.enums import ChatType
from aiogram.filters import Filter
from aiogram.types import CallbackQuery, Message


class PrivateChat(Filter):
    async def __call__(self, event: Message | CallbackQuery) -> bool:
        if isinstance(event, Message):
            return event.chat.type == ChatType.PRIVATE
        if event.message and event.message.chat:
            return event.message.chat.type == ChatType.PRIVATE
        return True
