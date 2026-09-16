"""Hidden AI job: shared program structure review (no per-athlete data)."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from datetime import datetime, timezone
from typing import Any

from aiogram import Bot
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app import ui_copy as ui
from app.config import get_settings
from app.db.models import AppSetting, ScheduleDay, WorkoutTemplate
from app.db.session import SessionLocal
from app.services.nn_client import NnStatus, get_nn_status, request_coach
from app.services.reminders import WEEKDAY_NAMES

logger = logging.getLogger("gymflex.program_review")

KIND = "program_review"
HIDDEN_TITLE = "Разбор программы"

SETTING_ADVICE = "program_review_advice"
SETTING_FINGERPRINT = "program_review_fingerprint"
SETTING_META = "program_review_meta"
SETTING_LAST_PREVIEW = "hidden_program_review_last_preview"
SETTING_RUN_HISTORY = "hidden_program_review_run_history"
HISTORY_MAX = 20
PREVIEW_CHUNK = 3500
MAX_TOKENS = 2048

ALARM_EMOJI = ui.PROGRAM_ALARM_EMOJI

_job_lock = asyncio.Lock()
_job_running: dict[str, Any] | None = None


def alarm_emoji(level: int | None) -> str:
    try:
        n = int(level) if level is not None else 3
    except (TypeError, ValueError):
        n = 3
    n = max(1, min(5, n))
    return ALARM_EMOJI.get(n, ALARM_EMOJI[3])


def clamp_alarm_level(raw: object) -> int:
    try:
        n = int(raw)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 3
    return max(1, min(5, n))


def parse_review_response(raw: str) -> tuple[str, int]:
    """Return (formatted_plain_advice, alarm_level)."""
    from app.services.week_plan import extract_json_object

    data = extract_json_object(raw)
    if not data or not any(
        data.get(k) for k in ("coverage", "order", "volume", "risks", "improvements")
    ):
        text = (raw or "").strip()
        return text, 3

    level = clamp_alarm_level(data.get("alarm_level"))
    emoji = alarm_emoji(level)
    sections = [
        ("Покрытие / баланс", data.get("coverage")),
        ("Порядок", data.get("order")),
        ("Объём", data.get("volume")),
        ("Риски", data.get("risks")),
        ("Что улучшить", data.get("improvements")),
    ]
    lines = [f"{emoji} Уровень тревоги: {level}/5", ""]
    for title, body in sections:
        body_s = str(body or "").strip()
        if not body_s:
            continue
        lines.append(title)
        lines.append(body_s)
        lines.append("")
    advice = "\n".join(lines).strip()
    return advice, level


def format_advice_html(advice: str | None) -> str:
    """Structured advice → Telegram HTML (section titles bold)."""
    text = (advice or "").strip()
    if not text:
        return ""
    section_titles = {
        "Покрытие / баланс",
        "Порядок",
        "Объём",
        "Риски",
        "Что улучшить",
    }
    out: list[str] = []
    for line in text.split("\n"):
        stripped = line.strip()
        if stripped in section_titles:
            out.append(f"<b>{ui.esc(stripped)}</b>")
        elif stripped.startswith("✅") or stripped.startswith("🟡") or stripped.startswith(
            "⚠️"
        ) or stripped.startswith("🟠") or stripped.startswith("☠️"):
            # alarm header line
            out.append(f"<b>{ui.esc(stripped)}</b>")
        elif stripped.startswith("Уровень тревоги") or "Уровень тревоги:" in stripped:
            out.append(f"<b>{ui.esc(stripped)}</b>")
        else:
            out.append(ui.coach_html(line) if line else "")
    return "\n".join(out)


def is_program_review_running() -> bool:
    return _job_running is not None


def get_program_review_run_status() -> dict[str, Any] | None:
    if _job_running is None:
        return None
    return dict(_job_running)


def any_hidden_job_running() -> bool:
    from app.services.week_plan import is_week_plan_running

    return is_week_plan_running() or is_program_review_running()


async def build_program_payload(session: AsyncSession) -> dict[str, Any]:
    schedule_rows = list(
        (
            await session.execute(
                select(ScheduleDay).options(selectinload(ScheduleDay.template))
            )
        ).scalars().all()
    )
    by_wd = {s.weekday: s for s in schedule_rows}
    schedule: list[dict[str, Any]] = []
    for wd in range(7):
        s = by_wd.get(wd)
        tpl = s.template if s else None
        if tpl:
            schedule.append(
                {
                    "wd": wd,
                    "day": WEEKDAY_NAMES[wd],
                    "template_id": tpl.id,
                    "template": tpl.name,
                    "hashtag": tpl.hashtag,
                }
            )
        else:
            schedule.append(
                {
                    "wd": wd,
                    "day": WEEKDAY_NAMES[wd],
                    "rest": True,
                }
            )

    templates_db = list(
        (
            await session.execute(
                select(WorkoutTemplate).options(selectinload(WorkoutTemplate.exercises))
            )
        ).scalars().all()
    )
    templates: list[dict[str, Any]] = []
    for tpl in templates_db:
        exercises = []
        for ex in tpl.exercises or []:
            exercises.append(
                {
                    "id": ex.id,
                    "position": ex.position,
                    "name": ex.name,
                    "machine_name": ex.machine_name,
                    "target_sets": ex.target_sets,
                    "target_reps_min": ex.target_reps_min,
                    "target_reps_max": ex.target_reps_max,
                    "weight_step": ex.weight_step,
                }
            )
        templates.append(
            {
                "id": tpl.id,
                "name": tpl.name,
                "hashtag": tpl.hashtag,
                "exercises": exercises,
            }
        )

    return {
        "kind": KIND,
        "schedule": schedule,
        "templates": templates,
    }


def program_fingerprint(payload: dict[str, Any]) -> str:
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


async def _get_setting(session: AsyncSession, key: str) -> str | None:
    row = await session.get(AppSetting, key)
    if row and (row.value or "").strip():
        return row.value.strip()
    return None


async def _set_setting(session: AsyncSession, key: str, value: str) -> None:
    row = await session.get(AppSetting, key)
    if row is None:
        session.add(AppSetting(key=key, value=value))
    else:
        row.value = value


async def load_stored_advice(session: AsyncSession) -> str | None:
    return await _get_setting(session, SETTING_ADVICE)


async def load_stored_fingerprint(session: AsyncSession) -> str | None:
    return await _get_setting(session, SETTING_FINGERPRINT)


async def load_meta(session: AsyncSession) -> dict[str, Any]:
    raw = await _get_setting(session, SETTING_META)
    if not raw:
        return {}
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    return data if isinstance(data, dict) else {}


async def load_run_history(session: AsyncSession) -> list[dict[str, Any]]:
    raw = await _get_setting(session, SETTING_RUN_HISTORY)
    if not raw:
        return []
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return []
    return data if isinstance(data, list) else []


async def append_run_history(session: AsyncSession, entry: dict[str, Any]) -> None:
    hist = await load_run_history(session)
    hist.insert(0, entry)
    hist = hist[:HISTORY_MAX]
    await _set_setting(session, SETTING_RUN_HISTORY, json.dumps(hist, ensure_ascii=False))
    await session.commit()


async def load_alarm_level(session: AsyncSession) -> int | None:
    meta = await load_meta(session)
    if not meta:
        return None
    if "alarm_level" not in meta:
        return None
    return clamp_alarm_level(meta.get("alarm_level"))


async def build_program_card_text(session: AsyncSession) -> tuple[str, int | None]:
    """Full program card HTML + alarm_level if advice exists."""
    schedule = list(
        (
            await session.execute(
                select(ScheduleDay).options(
                    selectinload(ScheduleDay.template).selectinload(
                        WorkoutTemplate.exercises
                    )
                )
            )
        ).scalars().all()
    )
    templates = list(
        (
            await session.execute(
                select(WorkoutTemplate).options(selectinload(WorkoutTemplate.exercises))
            )
        ).scalars().all()
    )
    advice = await load_stored_advice(session)
    if not advice:
        alarm = None
    else:
        alarm = await load_alarm_level(session)
        if alarm is None:
            alarm = 3

    by_day = {s.weekday: s.template for s in schedule}
    lines = [f"{ui.b(ui.BTN_PROGRAM)} График недели (read-only):"]
    for weekday in range(7):
        tpl = by_day.get(weekday)
        label = ui.b(tpl.name) if tpl else "отдых"
        lines.append(f"• {ui.b(WEEKDAY_NAMES[weekday])}: {label}")

    lines.append("")
    lines.append(f"{ui.b('Шаблоны:')}")
    for tpl in templates:
        lines.append("")
        lines.append(_format_template_html(tpl))

    return "\n".join(lines), alarm


def _format_template_html(template: Any) -> str:
    lines = [f"{ui.ICO_EXERCISE} {ui.b(template.name)} (#{ui.esc(template.hashtag)})"]
    if not template.exercises:
        lines.append("  (упражнений пока нет)")
    for ex in template.exercises:
        lines.append(
            f"  {ex.position + 1}. {ui.b(ex.name)} — "
            f"{ex.target_sets}×{ex.target_reps_min}-{ex.target_reps_max}, "
            f"шаг {ex.weight_step:g} кг"
        )
    return "\n".join(lines)


async def save_review_result(
    session: AsyncSession,
    *,
    advice: str,
    fingerprint: str,
    meta: dict[str, Any],
) -> None:
    await _set_setting(session, SETTING_ADVICE, advice.strip())
    await _set_setting(session, SETTING_FINGERPRINT, fingerprint)
    await _set_setting(session, SETTING_META, json.dumps(meta, ensure_ascii=False))
    await session.commit()


async def run_program_review(
    *,
    force: bool = False,
) -> dict[str, Any]:
    """Compute fingerprint, optionally call LLM, store advice. Returns report dict."""
    if await get_nn_status() != NnStatus.online:
        return {"ok": False, "error": "nn_offline", "skipped": False}

    async with SessionLocal() as session:
        payload = await build_program_payload(session)
        fp = program_fingerprint(payload)
        stored_fp = await load_stored_fingerprint(session)
        advice_prev = await load_stored_advice(session)

        if not force:
            if stored_fp and stored_fp == fp:
                return {
                    "ok": True,
                    "skipped": True,
                    "reason": "fingerprint_unchanged",
                    "fingerprint": fp,
                    "advice": advice_prev,
                }

        text = await request_coach(
            kind=KIND,
            athlete=payload,
            focus={},
            history=[],
            user_id=None,
            user_label="program",
            max_tokens=MAX_TOKENS,
        )

        settings = get_settings()
        model = (settings.program_review_model or "").strip() or None
        now = datetime.now(timezone.utc).isoformat()
        if not text:
            fail_meta = {
                "at": now,
                "ok": False,
                "error": "llm_empty",
                "fingerprint": fp,
                "force": force,
                "model": model,
            }
            await _set_setting(
                session, SETTING_META, json.dumps(fail_meta, ensure_ascii=False)
            )
            await session.commit()
            return {
                "ok": False,
                "skipped": False,
                "error": "llm_empty",
                "fingerprint": fp,
                "request_preview": json.dumps(payload, ensure_ascii=False, indent=2)[:8000],
            }

        advice, alarm_level = parse_review_response(text)
        ok_meta = {
            "at": now,
            "ok": True,
            "fingerprint": fp,
            "force": force,
            "model": model,
            "chars": len(advice),
            "alarm_level": alarm_level,
        }
        await save_review_result(session, advice=advice, fingerprint=fp, meta=ok_meta)
        return {
            "ok": True,
            "skipped": False,
            "fingerprint": fp,
            "advice": advice,
            "alarm_level": alarm_level,
            "meta": ok_meta,
            "request_preview": json.dumps(payload, ensure_ascii=False, indent=2)[:8000],
            "response_preview": text,
        }


async def maybe_run_scheduled_program_review(bot: Bot | None = None) -> None:
    """Weekly digest window; dedup via RecapSent; skip if fingerprint unchanged."""
    global _job_running
    _ = bot
    from app.services.reminders import _already_sent, _mark_sent

    settings = get_settings()
    from zoneinfo import ZoneInfo

    tz = ZoneInfo(settings.timezone)
    today = datetime.now(tz).date()

    async with SessionLocal() as session:
        if await _already_sent(session, 0, today, "ai_program_review"):
            return

    if _job_running is not None or _job_lock.locked():
        logger.info("program_review scheduled skipped — already running")
        return

    async with _job_lock:
        _job_running = {
            "scope": "cron",
            "started_at": datetime.now(timezone.utc).isoformat(),
        }
        report: dict[str, Any] = {}
        try:
            report = await run_program_review(force=False)
            async with SessionLocal() as session:
                await append_run_history(
                    session,
                    {
                        "at": datetime.now(timezone.utc).isoformat(),
                        "job": KIND,
                        "scope": "cron",
                        "ok": bool(report.get("ok")),
                        "skipped": bool(report.get("skipped")),
                        "reason": report.get("reason") or report.get("error"),
                        "fingerprint": (report.get("fingerprint") or "")[:12],
                    },
                )
                if report.get("ok") or report.get("skipped"):
                    await _mark_sent(session, 0, today, "ai_program_review")
        except Exception:
            logger.exception("scheduled program_review failed")
        finally:
            _job_running = None

    logger.info(
        "program_review scheduled: ok=%s skipped=%s reason=%s",
        report.get("ok"),
        report.get("skipped"),
        report.get("reason") or report.get("error"),
    )


async def force_program_review(
    bot: Bot,
    *,
    admin_telegram_id: int,
) -> tuple[str, dict[str, Any]]:
    global _job_running
    if _job_running is not None or _job_lock.locked():
        run = get_program_review_run_status() or {}
        return (
            f"Уже выполняется (с {run.get('started_at')}, {run.get('scope')}).",
            {},
        )

    async with _job_lock:
        _job_running = {
            "scope": "force",
            "started_at": datetime.now(timezone.utc).isoformat(),
            "admin_telegram_id": admin_telegram_id,
        }
        report: dict[str, Any] = {}
        try:
            report = await run_program_review(force=True)
            async with SessionLocal() as session:
                await append_run_history(
                    session,
                    {
                        "at": datetime.now(timezone.utc).isoformat(),
                        "job": KIND,
                        "scope": "force",
                        "ok": bool(report.get("ok")),
                        "skipped": False,
                        "reason": report.get("error"),
                        "fingerprint": (report.get("fingerprint") or "")[:12],
                    },
                )
                preview = {
                    "at": datetime.now(timezone.utc).isoformat(),
                    "ok": report.get("ok"),
                    "fingerprint": report.get("fingerprint"),
                    "advice": report.get("advice"),
                    "request_preview": report.get("request_preview"),
                    "response_preview": report.get("response_preview"),
                    "error": report.get("error"),
                }
                await _set_setting(
                    session,
                    SETTING_LAST_PREVIEW,
                    json.dumps(preview, ensure_ascii=False),
                )
                await session.commit()
        finally:
            _job_running = None

    if not report:
        return "Job оборвался без результата.", {}

    summary = format_program_review_summary(report)
    advice = (report.get("advice") or "").strip()
    header = f"🤫 Результат скрытого job: {HIDDEN_TITLE}"
    body = f"{header}\n\n{summary}"
    if advice:
        body += f"\n\n{ui.b('Совет ИИ')}\n{format_advice_html(advice)}"
    try:
        for i in range(0, len(body), 3500):
            await bot.send_message(admin_telegram_id, body[i : i + 3500])
    except Exception:
        logger.exception("failed to DM program_review result")

    return summary, report


def format_program_review_summary(report: dict[str, Any]) -> str:
    if report.get("error") == "nn_offline":
        return "ИИ офлайн — program_review не запущен"
    if report.get("skipped"):
        return (
            f"Пропущено: программа не менялась "
            f"({report.get('reason') or 'fingerprint'})."
        )
    if not report.get("ok"):
        return f"Ошибка: {report.get('error') or '?'}"
    fp = str(report.get("fingerprint") or "")[:12]
    chars = len(report.get("advice") or "")
    level = report.get("alarm_level")
    alarm = f" · {alarm_emoji(level)} {level}/5" if level is not None else ""
    return f"OK · fingerprint {fp}… · совет {chars} симв.{alarm}"


async def load_last_force_preview(session: AsyncSession) -> dict[str, Any] | None:
    raw = await _get_setting(session, SETTING_LAST_PREVIEW)
    if not raw:
        return None
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return None
    return data if isinstance(data, dict) else None


def format_advice_view(advice: str | None, *, alarm_level: int | None = None) -> str:
    """Full tip screen HTML."""
    emoji = alarm_emoji(alarm_level)
    header = f"{ui.b(ui.BTN_PROGRAM_AI)} {emoji}"
    body = format_advice_html(advice)
    if not body:
        return f"{header}\n\nПока нет сохранённого разбора."
    return f"{header}\n\n{body}"