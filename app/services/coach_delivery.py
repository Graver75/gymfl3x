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
from app.services.nn_client import (
    NnStatus,
    fetch_nn_meta,
    get_nn_status,
    request_coach,
    status_label,
)
from app.services.nn_dialog import (
    append_dialog_turn,
    history_for_api,
    load_dialog_history,
)

logger = logging.getLogger("gymflex.coach")


def format_nn_status_line(status: NnStatus) -> str:
    return f"{ui.ICO_NN} Нейросеть: {status_label(status)}"


async def format_prompt_info_text(*, turns: int = 0) -> str:
    meta = await fetch_nn_meta()
    system = str(meta.get("system_prompt") or "").strip()
    schema = str(meta.get("data_schema_ru") or "").strip()
    model = meta.get("model") or "?"
    provider = meta.get("provider") or "?"
    from app.services.coach_prompts import DEFAULT_TASKS

    task_lines = "\n".join(f"· {k}" for k in DEFAULT_TASKS)
    parts = [
        f"{ui.ICO_NN} <b>Что уходит в нейросеть</b>",
        f"Провайдер: <code>{html.escape(str(provider))}</code> · "
        f"модель: <code>{html.escape(str(model))}</code>",
        f"Реплик в вашем диалоге: {turns} (без системного промпта)",
        "",
        "<b>Системный промпт (Бендер)</b>:",
        f"<pre>{html.escape(system[:2200])}</pre>",
        "",
        "<b>Типы задач</b>:",
        html.escape(task_lines),
        "",
        "<b>Данные в JSON</b>",
        f"<pre>{html.escape(schema[:1200])}</pre>",
        "",
        "Админ: Нагрузка NN → Промпты ИИ — полный текст задач.",
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
    fsm_state: Any | None = None,
) -> None:
    """Fetch coach text and send/edit a Telegram message. Never raises."""
    try:
        status = await get_nn_status(force=True)
        if status != NnStatus.online:
            text = (
                f"{format_nn_status_line(status)}\n"
                "Разбор недоступен — коуч выключен, нет ключа, офлайн или квота."
            )
            if waiting_message is not None:
                await waiting_message.edit_text(text)
            else:
                await bot.send_message(chat_id, text)
            return

        if waiting_message is None:
            waiting_message = await bot.send_message(
                chat_id, f"{ui.ICO_NN} Готовлю разбор…"
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

            if kind == "session" or kind == "live_set":
                # Fresh JSON is enough; past tips only inflate tokens / echo
                history: list = []
            else:
                history = history_for_api(await load_dialog_history(session, user_id))
            focus: dict[str, Any] = {
                "session_id": focus_session_id,
                "exercise_id": focus_exercise_id,
                "exercise_name": focus_exercise_name,
            }
            # Do not merge live into focus — avoids duplicate payload in prompt/JSON
            user_meta = athlete.get("user") or {}
            user_label = user_meta.get("code") or user_meta.get("short_code")

        raw = await request_coach(
            kind=kind,
            athlete=athlete,
            focus=focus,
            history=history,
            user_id=user_id,
            user_label=user_label,
        )
        if not raw:
            await waiting_message.edit_text(
                f"{ui.ICO_NN} Не удалось получить разбор (таймаут, квота или провайдер)."
            )
            return

        if kind == "live_set":
            from app.services.week_plan import parse_gf_next

            raw, suggest, explicit_none = parse_gf_next(raw)
            if not (raw or "").strip():
                raw = "Без текста — только служебная метка. Продолжай лог."
            if fsm_state is not None:
                try:
                    if explicit_none:
                        await fsm_state.update_data(live_suggest=None)
                    elif suggest is not None:
                        for_set = None
                        if live and live.get("current_set") is not None:
                            try:
                                for_set = int(live["current_set"])
                            except (TypeError, ValueError):
                                for_set = None
                        payload = dict(suggest)
                        if for_set is not None:
                            payload["for_set"] = for_set
                        fsm_updates: dict[str, Any] = {"live_suggest": payload}
                        # Mirror tip into draft so user doesn't log old weight by inertia
                        if suggest.get("kg") is not None:
                            fsm_updates["draft_weight"] = float(suggest["kg"])
                            fsm_updates["ai_plan_kg"] = float(suggest["kg"])
                        if suggest.get("reps") is not None:
                            fsm_updates["ai_plan_reps"] = int(suggest["reps"])
                            fsm_updates["smart_default_reps"] = int(suggest["reps"])
                        if suggest.get("rpe") is not None:
                            fsm_updates["ai_plan_rpe"] = int(suggest["rpe"])
                        await fsm_state.update_data(**fsm_updates)
                except Exception:
                    logger.exception("Failed to store live_suggest in FSM")

        if kind == "live_set" and live:
            user_summary = (
                f"[live_set] ex={live.get('exercise_name') or focus_exercise_name} "
                f"set={live.get('current_set')} kg={live.get('draft_weight')} "
                f"done={live.get('sets_done')}"
            )
        else:
            user_summary = (
                f"[разбор:{kind}] "
                f"ex={focus_exercise_name or focus.get('exercise_name')} "
                f"sid={focus_session_id} "
                f"sessions={len((athlete.get('sessions') or []))} "
                f"window={athlete.get('window_days')}"
            )
        try:
            async with SessionLocal() as session:
                await append_dialog_turn(
                    session, user_id, role="user", content=user_summary, kind=kind
                )
                await append_dialog_turn(
                    session, user_id, role="assistant", content=raw, kind=kind
                )
                await session.commit()
        except Exception:
            logger.exception("Dialog history save failed (reply still sent)")

        safe = ui.coach_html(raw)
        if kind == "live_set":
            title = "Совет по подходу"
        elif kind == "session":
            title = "Разбор тренировки"
        elif kind == "week":
            title = "Недельный разбор"
        else:
            title = "ИИ-разбор"
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
