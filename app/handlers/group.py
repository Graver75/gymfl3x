from __future__ import annotations

from aiogram import Bot, Router
from aiogram.filters import ChatMemberUpdatedFilter, IS_MEMBER, IS_NOT_MEMBER
from aiogram.types import ChatMemberUpdated
from sqlalchemy import select

from app.config import get_settings
from app.db.models import GroupChat
from app.db.session import SessionLocal
from app import ui_copy as ui

router = Router(name="group")


@router.my_chat_member(ChatMemberUpdatedFilter(IS_NOT_MEMBER >> IS_MEMBER))
async def bot_added_to_group(event: ChatMemberUpdated, bot: Bot) -> None:
    chat = event.chat
    if chat.type not in {"group", "supergroup"}:
        return

    settings = get_settings()
    async with SessionLocal() as session:
        existing = (
            await session.execute(select(GroupChat).where(GroupChat.chat_id == chat.id))
        ).scalar_one_or_none()
        if existing:
            existing.active = True
            existing.title = chat.title
        else:
            session.add(
                GroupChat(
                    chat_id=chat.id,
                    title=chat.title,
                    reminder_hour=settings.reminder_hour,
                    recap_hour=settings.recap_hour,
                    active=True,
                )
            )
        await session.commit()

    try:
        await bot.send_message(
            chat.id,
            f"{ui.ICO_FIRE} Gymflex на связи.\n"
            "Утром в тренировочный день напомню программу,\n"
            "вечером пришлю сводку в формате #деньспины.\n"
            "Логируйте подходы в личке с ботом.",
        )
    except Exception:
        pass


@router.my_chat_member(ChatMemberUpdatedFilter(IS_MEMBER >> IS_NOT_MEMBER))
async def bot_removed_from_group(event: ChatMemberUpdated) -> None:
    chat = event.chat
    if chat.type not in {"group", "supergroup"}:
        return
    async with SessionLocal() as session:
        existing = (
            await session.execute(select(GroupChat).where(GroupChat.chat_id == chat.id))
        ).scalar_one_or_none()
        if existing:
            existing.active = False
            await session.commit()
