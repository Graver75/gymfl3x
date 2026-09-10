"""Helpers to request and deliver LLM coach feedback."""

from __future__ import annotations

import html
import logging
from typing import Any

from aiogram import Bot
from aiogram.types import Message

from app import ui_copy as ui
from app.db.session import SessionLocal
from app.services.coach_context import build_coach_context
from app.services.nn_client import NnStatus, fetch_nn_meta, get_nn_status, request_coach, status_label
from app.services.nn_dialog import (
    append_dialog_turn,
    load_dialog_history,
    history_for_api,
)

logger = logging.getLogger("gymflex.coach")


def format_nn_status_line(status: NnStatus) -> str:
    return f"{ui.ICO_NN} Нейросеть: {status_label(status)}"


async def format_prompt_info_text(*, turns: int = 0) -> str:
    meta = await fetch_nn_meta()
    system = str(meta.get("system_prompt") or "").strip()
    schema = str(meta.get("data_schema_ru") or "").strip()
    model = meta.get("model") or "?"
    parts = [
        f"{ui.ICO_NN} <b>Что уходит в нейросеть</b>",
        f"Модель: <code>{html.escape(str(model))}</code>",
        f"Реплик в вашем диалоге: {turns} (без системного промпта)",
        "",
        "<b>Системный промпт</b> (всегда первый, общий для коуча):",
        f"<pre>{html.escape(system[:2800])}</pre>",
        "",
        "<b>Данные атлета в каждом запросе</b>",
        html.escape(schema[:1500]),
        "",
        "Диалог только ваш. Очистка сбрасывает историю до системного промпта.",
    ]
    text = "\n".join(parts)
    if len(text) > 4000:
        text = text[:3990] + "…"
    return text


async def run_coach_and_reply(
    *,
    bot: Bot,
    chat_id: int,
    user_id: int,
    kind: str,
    focus_session_id: int | None = None,
    focus_exercise_id: int | None = None,
    focus_exercise_name: str | None = None,
    live: dict[str, Any] | None = None,
    waiting_message: Message | None = None,
) -> None:
    """Fetch coach text and send/edit a Telegram message. Never raises."""
    try:
        status = await get_nn_status(force=True)
        if status != NnStatus.online:
            text = (
                f"{format_nn_status_line(status)}\n"
                "Разбор недоступен — сервис не запущен или выключен."
            )
            if waiting_message is not None:
                await waiting_message.edit_text(text)
            else:
                await bot.send_message(chat_id, text)
            return

        if waiting_message is None:
            waiting_message = await bot.send_message(
                chat_id, f"{ui.ICO_NN} Готовлю разбор… Это может занять до пары минут."
            )

        async with SessionLocal() as session:
            athlete = await build_coach_context(
                session,
                user_id,
                kind=kind,
                focus_session_id=focus_session_id,
                focus_exercise_id=focus_exercise_id,
                focus_exercise_name=focus_exercise_name,
                live=live,
            )
            if athlete.get("error"):
                await waiting_message.edit_text("Не удалось собрать данные для разбора.")
                return

            history = await load_dialog_history(session, user_id)
            focus: dict[str, Any] = {
                "session_id": focus_session_id,
                "exercise_id": focus_exercise_id,
                "exercise_name": focus_exercise_name,
            }
            if live:
                focus.update(live)
            user_meta = athlete.get("user") or {}
            user_label = user_meta.get("code") or user_meta.get("short_code")

        raw = await request_coach(
            kind=kind,
            athlete=athlete,
            focus=focus,
            history=history_for_api(history),
            user_id=user_id,
            user_label=user_label,
        )
        if not raw:
            await waiting_message.edit_text(
                f"{ui.ICO_NN} Не удалось получить разбор (таймаут или сервис недоступен)."
            )
            return

        # Persist turn: store a short user summary + full assistant reply
        user_summary = (
            f"[разбор:{kind}] focus={focus} "
            f"sessions={len((athlete.get('sessions') or []))} "
            f"window={athlete.get('window_days')}"
        )
        async with SessionLocal() as session:
            await append_dialog_turn(
                session, user_id, role="user", content=user_summary, kind=kind
            )
            # Also keep a compact copy of what was asked (without huge JSON) —
            # the next call rebuilds fresh athlete JSON; history is conversational.
            await append_dialog_turn(
                session, user_id, role="assistant", content=raw, kind=kind
            )
            await session.commit()

        safe = html.escape(raw)
        title = "Совет по подходу" if kind == "live_set" else "ИИ-разбор"
        body = f"{ui.ICO_NN} <b>{title}</b>\n\n{safe}"
        if len(body) > 4000:
            body = body[:3990] + "…"
        await waiting_message.edit_text(body)
    except Exception:
        logger.exception("Coach delivery failed")
        try:
            if waiting_message is not None:
                await waiting_message.edit_text(
                    f"{ui.ICO_NN} Ошибка при разборе. Попробуй позже."
                )
        except Exception:
            pass