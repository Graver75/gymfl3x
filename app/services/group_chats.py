"""Known chats (groups + private users) and live Telegram write probes."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

from aiogram import Bot
from aiogram.enums import ChatMemberStatus, ChatType
from aiogram.types import ChatMember
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import GroupChat, User

Kind = Literal["group", "private"]


@dataclass
class ChatProbe:
    kind: Kind
    chat_id: int
    title: str | None
    in_chat: bool
    can_write: bool
    status: str
    detail: str | None = None
    short_code: str | None = None

    @property
    def button_label(self) -> str:
        prefix = "👥" if self.kind == "group" else "👤"
        name = (self.title or "без названия").strip()
        if self.short_code:
            name = f"{name} ({self.short_code})"
        mark = "✍️" if self.can_write else "🚫"
        text = f"{mark} {prefix} {name}"
        return text[:58]


def _member_status(member: ChatMember) -> str:
    status = getattr(member, "status", None)
    if isinstance(status, ChatMemberStatus):
        return status.value
    return str(status or "?")


def _can_write_group(member: ChatMember) -> bool:
    status = getattr(member, "status", None)
    if status in {ChatMemberStatus.CREATOR, ChatMemberStatus.ADMINISTRATOR}:
        if hasattr(member, "can_post_messages") and member.can_post_messages is False:
            return False
        return True
    if status == ChatMemberStatus.MEMBER:
        return True
    if status == ChatMemberStatus.RESTRICTED:
        return bool(getattr(member, "can_send_messages", False))
    return False


async def list_known_groups(session: AsyncSession) -> list[GroupChat]:
    result = await session.execute(
        select(GroupChat).order_by(GroupChat.active.desc(), GroupChat.id.asc())
    )
    return list(result.scalars().all())


async def list_known_users(session: AsyncSession) -> list[User]:
    result = await session.execute(select(User).order_by(User.display_name.asc(), User.id.asc()))
    return list(result.scalars().all())


async def probe_group(bot: Bot, row: GroupChat) -> ChatProbe:
    title = row.title
    try:
        chat = await bot.get_chat(row.chat_id)
        title = chat.title or title
        if chat.type not in {ChatType.GROUP, ChatType.SUPERGROUP}:
            return ChatProbe(
                kind="group",
                chat_id=row.chat_id,
                title=title,
                in_chat=False,
                can_write=False,
                status=str(chat.type),
                detail="не группа",
            )
    except Exception as exc:
        return ChatProbe(
            kind="group",
            chat_id=row.chat_id,
            title=title,
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
            kind="group",
            chat_id=row.chat_id,
            title=title,
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
    can_write = in_chat and _can_write_group(member)
    return ChatProbe(
        kind="group",
        chat_id=row.chat_id,
        title=title,
        in_chat=in_chat,
        can_write=can_write,
        status=status,
        detail=None,
    )


async def probe_private(bot: Bot, user: User) -> ChatProbe:
    title = user.display_name
    try:
        chat = await bot.get_chat(user.telegram_id)
        if chat.type != ChatType.PRIVATE:
            return ChatProbe(
                kind="private",
                chat_id=user.telegram_id,
                title=title,
                in_chat=False,
                can_write=False,
                status=str(chat.type),
                detail="не личка",
                short_code=user.short_code,
            )
        # Prefer Telegram full name if available
        parts = [chat.first_name or "", chat.last_name or ""]
        tg_name = " ".join(p for p in parts if p).strip()
        if tg_name:
            title = tg_name
        # getChat succeeds → usually can message (until user blocks)
        return ChatProbe(
            kind="private",
            chat_id=user.telegram_id,
            title=title,
            in_chat=True,
            can_write=True,
            status="private",
            detail=None,
            short_code=user.short_code,
        )
    except Exception as exc:
        return ChatProbe(
            kind="private",
            chat_id=user.telegram_id,
            title=title,
            in_chat=False,
            can_write=False,
            status="unknown",
            detail=f"getChat: {type(exc).__name__}",
            short_code=user.short_code,
        )


async def refresh_destinations(bot: Bot, session: AsyncSession) -> list[ChatProbe]:
    probes: list[ChatProbe] = []
    for row in await list_known_groups(session):
        probe = await probe_group(bot, row)
        probes.append(probe)
        if probe.title and probe.title != row.title:
            row.title = probe.title
        row.active = probe.in_chat
    for user in await list_known_users(session):
        probes.append(await probe_private(bot, user))
    await session.commit()
    # Groups first, then private; writable first within kind
    probes.sort(
        key=lambda p: (
            0 if p.kind == "group" else 1,
            0 if p.can_write else 1,
            (p.title or "").lower(),
        )
    )
    return probes


# Back-compat alias
async def refresh_chats(bot: Bot, session: AsyncSession) -> list[ChatProbe]:
    return await refresh_destinations(bot, session)


def format_chats_report(probes: list[ChatProbe]) -> str:
    lines = [
        "💬 <b>Чаты бота</b>",
        "Группы (куда добавляли) и люди (кто писал боту).",
        "Нажми кнопку ниже — написать в чат. 🚫 = сейчас писать нельзя.",
        "",
    ]
    if not probes:
        lines.append("Пока пусто. Добавь бота в группу или пусть кто-то напишет /start.")
        return "\n".join(lines)

    groups = [p for p in probes if p.kind == "group"]
    people = [p for p in probes if p.kind == "private"]

    lines.append(f"<b>Группы ({len(groups)})</b>")
    if not groups:
        lines.append("— нет —")
    for p in groups:
        lines.extend(_format_probe_lines(p))
    lines.append("")
    lines.append(f"<b>Люди ({len(people)})</b>")
    if not people:
        lines.append("— нет —")
    for p in people:
        lines.extend(_format_probe_lines(p))
    return "\n".join(lines).rstrip()


def _format_probe_lines(p: ChatProbe) -> list[str]:
    name = p.title or "без названия"
    if p.short_code:
        name = f"{name} · {p.short_code}"
    in_mark = "✅ в чате" if p.in_chat else "❌ нет доступа"
    write_mark = "✅ можно писать" if p.can_write else "❌ писать нельзя"
    out = [
        f"• <b>{_esc(name)}</b>",
        f"  id <code>{p.chat_id}</code>",
        f"  {in_mark} · {write_mark} · <code>{_esc(p.status)}</code>",
    ]
    if p.detail:
        out.append(f"  <i>{_esc(p.detail)}</i>")
    return out


def _esc(text: object) -> str:
    from html import escape

    return escape(str(text), quote=False)


def probes_summary(probes: list[ChatProbe]) -> dict[str, Any]:
    return {
        "total": len(probes),
        "groups": sum(1 for p in probes if p.kind == "group"),
        "private": sum(1 for p in probes if p.kind == "private"),
        "can_write": sum(1 for p in probes if p.can_write),
    }
