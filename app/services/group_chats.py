"""Group chats known to the bot + live Telegram membership / write checks."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from aiogram import Bot
from aiogram.enums import ChatMemberStatus, ChatType
from aiogram.types import ChatMember
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import GroupChat


@dataclass
class ChatProbe:
    chat_id: int
    title: str | None
    db_active: bool
    in_chat: bool
    can_write: bool
    status: str
    detail: str | None = None


def _member_status(member: ChatMember) -> str:
    status = getattr(member, "status", None)
    if isinstance(status, ChatMemberStatus):
        return status.value
    return str(status or "?")


def _can_write(member: ChatMember) -> bool:
    status = getattr(member, "status", None)
    if status in {ChatMemberStatus.CREATOR, ChatMemberStatus.ADMINISTRATOR}:
        # Channels: need can_post_messages; groups/supergroups: admin can send
        if hasattr(member, "can_post_messages") and member.can_post_messages is False:
            return False
        return True
    if status == ChatMemberStatus.MEMBER:
        return True
    if status == ChatMemberStatus.RESTRICTED:
        return bool(getattr(member, "can_send_messages", False))
    return False


async def list_known_chats(session: AsyncSession) -> list[GroupChat]:
    result = await session.execute(
        select(GroupChat).order_by(GroupChat.active.desc(), GroupChat.id.asc())
    )
    return list(result.scalars().all())


async def probe_chat(bot: Bot, row: GroupChat) -> ChatProbe:
    title = row.title
    try:
        chat = await bot.get_chat(row.chat_id)
        title = chat.title or title
        if chat.type not in {ChatType.GROUP, ChatType.SUPERGROUP}:
            return ChatProbe(
                chat_id=row.chat_id,
                title=title,
                db_active=row.active,
                in_chat=False,
                can_write=False,
                status=str(chat.type),
                detail="не группа",
            )
    except Exception as exc:
        return ChatProbe(
            chat_id=row.chat_id,
            title=title,
            db_active=row.active,
            in_chat=False,
            can_write=False,
            status="unknown",
            detail=f"getChat: {type(exc).__name__}",
        )

    me = await bot.get_me()
    try:
        member = await bot.get_chat_member(row.chat_id, me.id)
    except Exception as exc:
        return ChatProbe(
            chat_id=row.chat_id,
            title=title,
            db_active=row.active,
            in_chat=False,
            can_write=False,
            status="unknown",
            detail=f"getChatMember: {type(exc).__name__}",
        )

    status = _member_status(member)
    in_chat = status not in {
        ChatMemberStatus.LEFT.value,
        ChatMemberStatus.KICKED.value,
    }
    can_write = in_chat and _can_write(member)
    return ChatProbe(
        chat_id=row.chat_id,
        title=title,
        db_active=row.active,
        in_chat=in_chat,
        can_write=can_write,
        status=status,
        detail=None,
    )


async def refresh_chats(bot: Bot, session: AsyncSession) -> list[ChatProbe]:
    rows = await list_known_chats(session)
    probes: list[ChatProbe] = []
    for row in rows:
        probe = await probe_chat(bot, row)
        probes.append(probe)
        if probe.title and probe.title != row.title:
            row.title = probe.title
        row.active = probe.in_chat
    await session.commit()
    return probes


def format_chats_report(probes: list[ChatProbe]) -> str:
    lines = [
        "💬 <b>Чаты бота</b>",
        "Источник: группы, куда бота добавляли (или удаляли).",
        "Статус и «может писать» — живой запрос к Telegram.",
        "",
    ]
    if not probes:
        lines.append("Пока пусто. Добавь бота в групповой чат.")
        return "\n".join(lines)

    for p in probes:
        name = p.title or "без названия"
        in_mark = "✅ в чате" if p.in_chat else "❌ не в чате"
        write_mark = "✅ может писать" if p.can_write else "❌ писать нельзя"
        lines.append(f"• <b>{_esc(name)}</b>")
        lines.append(f"  id <code>{p.chat_id}</code>")
        lines.append(f"  {in_mark} · {write_mark} · status=<code>{_esc(p.status)}</code>")
        if p.detail:
            lines.append(f"  <i>{_esc(p.detail)}</i>")
        lines.append("")
    return "\n".join(lines).rstrip()


def _esc(text: object) -> str:
    from html import escape

    return escape(str(text), quote=False)


def probes_summary(probes: list[ChatProbe]) -> dict[str, Any]:
    return {
        "total": len(probes),
        "in_chat": sum(1 for p in probes if p.in_chat),
        "can_write": sum(1 for p in probes if p.can_write),
    }
