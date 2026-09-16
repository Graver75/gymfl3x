"""Hidden AI job: weekly per-exercise set prescriptions for athletes."""

from __future__ import annotations

import json
import logging
import re
from datetime import date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from aiogram import Bot
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.db.models import (
    TemplateExercise,
    User,
    UserExerciseState,
    UserExerciseWeekPlan,
)
from app.db.session import SessionLocal
from app.services.coach_context import build_coach_context
from app.services.nn_client import NnStatus, get_nn_status, request_coach
from app.services.reminders import get_template_for_weekday

logger = logging.getLogger("gymflex.week_plan")

HIDDEN_JOBS = {
    "week_plan": "Прогноз недели (план kg/reps/RPE)",
}


def monday_of(d: date) -> date:
    return d - timedelta(days=d.weekday())


def target_week_start(today: date | None = None) -> date:
    """Week the plan applies to: current Mon–Sun, or next week if Sat/Sun (digest day)."""
    today = today or date.today()
    this_monday = monday_of(today)
    if today.weekday() >= 5:  # Sat/Sun → upcoming week
        return this_monday + timedelta(days=7)
    return this_monday


def current_week_start(today: date | None = None) -> date:
    return monday_of(today or date.today())


def parse_sets_json(raw: str | None) -> list[dict[str, Any]]:
    if not raw:
        return []
    try:
        data = json.loads(raw)
    except (TypeError, json.JSONDecodeError):
        return []
    if not isinstance(data, list):
        return []
    out: list[dict[str, Any]] = []
    for row in data:
        if not isinstance(row, dict):
            continue
        try:
            n = int(row.get("n") or row.get("set") or 0)
            kg = float(row.get("kg") or row.get("weight") or 0)
            reps = int(row.get("reps") or 0)
            rpe = int(row.get("rpe") or row.get("rpe_1_10") or 0)
        except (TypeError, ValueError):
            continue
        if n < 1:
            continue
        item = {"n": n, "kg": kg, "reps": reps}
        if 1 <= rpe <= 10:
            item["rpe"] = rpe
        out.append(item)
    return sorted(out, key=lambda x: x["n"])


def set_for_number(sets: list[dict[str, Any]], set_number: int) -> dict[str, Any] | None:
    for row in sets:
        if int(row.get("n") or 0) == int(set_number):
            return row
    return None


def extract_json_object(text: str) -> dict[str, Any] | None:
    if not text:
        return None
    cleaned = text.strip()
    fence = re.search(r"```(?:json)?\s*([\s\S]*?)```", cleaned, re.IGNORECASE)
    if fence:
        cleaned = fence.group(1).strip()
    try:
        data = json.loads(cleaned)
        if isinstance(data, dict):
            return data
    except json.JSONDecodeError:
        pass
    start = cleaned.find("{")
    end = cleaned.rfind("}")
    if start >= 0 and end > start:
        try:
            data = json.loads(cleaned[start : end + 1])
            if isinstance(data, dict):
                return data
        except json.JSONDecodeError:
            return None
    return None


async def exercises_for_week(
    session: AsyncSession, week_start: date
) -> list[TemplateExercise]:
    """Unique template exercises from schedule Mon–Sun of week_start."""
    by_id: dict[int, TemplateExercise] = {}
    for offset in range(7):
        day = week_start + timedelta(days=offset)
        tpl = await get_template_for_weekday(session, day.weekday())
        if not tpl:
            continue
        for ex in tpl.exercises or []:
            by_id[ex.id] = ex
    return sorted(by_id.values(), key=lambda e: (e.template_id, e.position, e.id))


def _target_exercises_payload(exercises: list[TemplateExercise]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for ex in exercises:
        rows.append(
            {
                "exercise_id": ex.id,
                "name": ex.name,
                "machine_name": getattr(ex, "machine_name", None),
                "target_sets": ex.target_sets,
                "target_reps_min": ex.target_reps_min,
                "target_reps_max": ex.target_reps_max,
                "weight_step": ex.weight_step,
            }
        )
    return rows


async def get_plan(
    session: AsyncSession,
    *,
    user_id: int,
    exercise_id: int,
    week_start: date | None = None,
) -> UserExerciseWeekPlan | None:
    """Active plan: prefer current week, else next week if present."""
    weeks: list[date] = []
    if week_start is not None:
        weeks.append(week_start)
    else:
        cur = current_week_start()
        nxt = cur + timedelta(days=7)
        weeks.extend([cur, nxt])
    for ws in weeks:
        row = (
            await session.execute(
                select(UserExerciseWeekPlan).where(
                    UserExerciseWeekPlan.user_id == user_id,
                    UserExerciseWeekPlan.exercise_id == exercise_id,
                    UserExerciseWeekPlan.week_start == ws,
                )
            )
        ).scalar_one_or_none()
        if row:
            return row
    return None


def plan_to_live_blob(plan: UserExerciseWeekPlan | None) -> dict[str, Any] | None:
    if not plan:
        return None
    return {
        "week_start": plan.week_start.isoformat(),
        "advice": (plan.advice or "")[:500],
        "sets": parse_sets_json(plan.sets_json),
    }


def format_plan_card_html(plan: UserExerciseWeekPlan) -> str:
    import html as html_mod

    sets = parse_sets_json(plan.sets_json)
    lines = [
        f"<b>План ИИ</b> · неделя с {html_mod.escape(plan.week_start.isoformat())}"
    ]
    for s in sets:
        rpe = s.get("rpe")
        rpe_s = f" @RPE{rpe}" if rpe else ""
        lines.append(
            f"· подход {s['n']}: <b>{s['kg']:g}×{s['reps']}</b>{rpe_s}"
        )
    advice = (plan.advice or "").strip()
    if advice:
        lines.append("")
        lines.append(html_mod.escape(advice))
    return "\n".join(lines)


async def _upsert_plan(
    session: AsyncSession,
    *,
    user_id: int,
    exercise_id: int,
    week_start: date,
    advice: str | None,
    sets: list[dict[str, Any]],
    usage_log_id: int | None,
) -> None:
    existing = (
        await session.execute(
            select(UserExerciseWeekPlan).where(
                UserExerciseWeekPlan.user_id == user_id,
                UserExerciseWeekPlan.exercise_id == exercise_id,
                UserExerciseWeekPlan.week_start == week_start,
            )
        )
    ).scalar_one_or_none()
    payload = json.dumps(sets, ensure_ascii=False)
    if existing:
        existing.advice = advice
        existing.sets_json = payload
        existing.usage_log_id = usage_log_id
    else:
        session.add(
            UserExerciseWeekPlan(
                user_id=user_id,
                exercise_id=exercise_id,
                week_start=week_start,
                advice=advice,
                sets_json=payload,
                usage_log_id=usage_log_id,
            )
        )
    # Sync suggested_weight from first set
    if sets:
        kg = float(sets[0].get("kg") or 0)
        if kg > 0:
            st = (
                await session.execute(
                    select(UserExerciseState).where(
                        UserExerciseState.user_id == user_id,
                        UserExerciseState.exercise_id == exercise_id,
                    )
                )
            ).scalar_one_or_none()
            if st:
                st.suggested_weight = kg
            else:
                session.add(
                    UserExerciseState(
                        user_id=user_id,
                        exercise_id=exercise_id,
                        suggested_weight=kg,
                        working_weight=kg,
                    )
                )


async def run_week_plan_for_user(
    session: AsyncSession,
    user: User,
    *,
    week_start: date,
    exercises: list[TemplateExercise] | None = None,
) -> dict[str, Any]:
    """Call LLM for one athlete and upsert plans. Returns summary dict."""
    exs = exercises if exercises is not None else await exercises_for_week(session, week_start)
    if not exs:
        return {
            "user_id": user.id,
            "code": user.short_code,
            "ok": False,
            "error": "no_exercises",
            "saved": 0,
        }

    ctx = await build_coach_context(session, user.id, kind="week")
    payload = {
        **ctx,
        "kind": "week_plan",
        "week_start": week_start.isoformat(),
        "target_exercises": _target_exercises_payload(exs),
    }
    raw = await request_coach(
        kind="week_plan",
        athlete=payload,
        focus={"week_start": week_start.isoformat()},
        history=[],
        user_id=user.id,
        user_label=user.short_code,
    )
    if not raw:
        return {
            "user_id": user.id,
            "code": user.short_code,
            "ok": False,
            "error": "llm_empty",
            "saved": 0,
        }

    parsed = extract_json_object(raw)
    if not parsed:
        return {
            "user_id": user.id,
            "code": user.short_code,
            "ok": False,
            "error": "json_parse",
            "saved": 0,
            "raw_preview": raw[:400],
        }

    allowed = {ex.id for ex in exs}
    items = parsed.get("exercises")
    if not isinstance(items, list):
        return {
            "user_id": user.id,
            "code": user.short_code,
            "ok": False,
            "error": "bad_schema",
            "saved": 0,
            "raw_preview": raw[:400],
        }

    saved = 0
    for item in items:
        if not isinstance(item, dict):
            continue
        try:
            ex_id = int(item.get("exercise_id"))
        except (TypeError, ValueError):
            continue
        if ex_id not in allowed:
            continue
        sets_raw = item.get("sets") or []
        sets: list[dict[str, Any]] = []
        if isinstance(sets_raw, list):
            for s in sets_raw:
                if not isinstance(s, dict):
                    continue
                try:
                    n = int(s.get("n") or s.get("set") or 0)
                    kg = float(s.get("kg") or s.get("weight") or 0)
                    reps = int(s.get("reps") or 0)
                    rpe = int(s.get("rpe") or 0)
                except (TypeError, ValueError):
                    continue
                if n < 1 or kg <= 0 or reps <= 0:
                    continue
                row: dict[str, Any] = {"n": n, "kg": kg, "reps": reps}
                if 1 <= rpe <= 10:
                    row["rpe"] = rpe
                sets.append(row)
        sets = sorted(sets, key=lambda x: x["n"])
        if not sets:
            continue
        advice = item.get("advice")
        advice_s = str(advice).strip()[:1200] if advice else None
        await _upsert_plan(
            session,
            user_id=user.id,
            exercise_id=ex_id,
            week_start=week_start,
            advice=advice_s,
            sets=sets,
            usage_log_id=None,
        )
        saved += 1

    await session.commit()
    return {
        "user_id": user.id,
        "code": user.short_code,
        "ok": saved > 0,
        "saved": saved,
        "raw_preview": raw[:600],
    }


async def run_week_plan_batch(
    *,
    week_start: date | None = None,
    users: list[User] | None = None,
    only_user_id: int | None = None,
) -> dict[str, Any]:
    """Run week_plan for onboarded users (or one)."""
    settings = get_settings()
    tz = ZoneInfo(settings.timezone)
    today = datetime.now(tz).date()
    ws = week_start or target_week_start(today)

    if await get_nn_status() != NnStatus.online:
        return {"ok": False, "error": "nn_offline", "week_start": ws.isoformat(), "results": []}

    async with SessionLocal() as session:
        exercises = await exercises_for_week(session, ws)
        if not exercises:
            return {
                "ok": False,
                "error": "no_exercises",
                "week_start": ws.isoformat(),
                "results": [],
            }

        if users is None:
            q = select(User).where(User.onboarding_done.is_(True))
            if only_user_id is not None:
                q = q.where(User.id == only_user_id)
            users = list((await session.execute(q)).scalars().all())

        results: list[dict[str, Any]] = []
        for user in users:
            try:
                # Re-fetch exercises in same session after possible commits
                exs = await exercises_for_week(session, ws)
                res = await run_week_plan_for_user(
                    session, user, week_start=ws, exercises=exs
                )
                results.append(res)
            except Exception as exc:
                logger.exception("week_plan failed user=%s", user.id)
                results.append(
                    {
                        "user_id": user.id,
                        "code": user.short_code,
                        "ok": False,
                        "error": str(exc)[:200],
                        "saved": 0,
                    }
                )

        ok_n = sum(1 for r in results if r.get("ok"))
        saved_n = sum(int(r.get("saved") or 0) for r in results)
        return {
            "ok": ok_n > 0,
            "week_start": ws.isoformat(),
            "athletes": len(results),
            "ok_athletes": ok_n,
            "exercises_saved": saved_n,
            "target_exercise_n": len(exercises),
            "results": results,
        }


def format_batch_summary(report: dict[str, Any], *, max_preview: int = 1200) -> str:
    if report.get("error") == "nn_offline":
        return "ИИ офлайн — week_plan не запущен"
    if report.get("error") == "no_exercises":
        return f"Нет упражнений в расписании на неделю {report.get('week_start')}"

    lines = [
        f"Прогноз недели · week_start={report.get('week_start')}",
        f"Атлетов: {report.get('ok_athletes', 0)}/{report.get('athletes', 0)} ок",
        f"Упражнений записано: {report.get('exercises_saved', 0)} "
        f"(целевых в шаблонах: {report.get('target_exercise_n', 0)})",
    ]
    for r in report.get("results") or []:
        code = r.get("code") or r.get("user_id")
        if r.get("ok"):
            lines.append(f"· {code}: +{r.get('saved')} упр.")
        else:
            lines.append(f"· {code}: FAIL ({r.get('error')})")
        preview = r.get("raw_preview")
        if preview and not r.get("ok"):
            lines.append(f"  preview: {preview[:200]}")
    text = "\n".join(lines)
    # Attach one successful raw preview for admin inspection
    for r in report.get("results") or []:
        if r.get("ok") and r.get("raw_preview"):
            text += "\n\n--- JSON preview ---\n" + str(r["raw_preview"])[:max_preview]
            break
    if len(text) > 3900:
        text = text[:3890] + "…"
    return text


async def maybe_run_scheduled_week_plan(bot: Bot | None = None) -> None:
    """Same hour/weekday as week digests; dedup via RecapSent chat_id=0."""
    from app.services.reminders import _already_sent, _mark_sent

    settings = get_settings()
    tz = ZoneInfo(settings.timezone)
    now = datetime.now(tz)
    today = now.date()
    if not (
        now.hour == settings.week_digest_hour
        and today.weekday() == settings.week_digest_weekday
    ):
        return

    async with SessionLocal() as session:
        if await _already_sent(session, 0, today, "ai_week_plan"):
            return

    report = await run_week_plan_batch()
    async with SessionLocal() as session:
        await _mark_sent(session, 0, today, "ai_week_plan")

    logger.info(
        "week_plan scheduled: ok=%s athletes=%s saved=%s",
        report.get("ok"),
        report.get("athletes"),
        report.get("exercises_saved"),
    )
    if bot is not None and report.get("results"):
        # Optional: nothing to chat; admin sees via force / logs
        _ = bot


async def force_week_plan(
    bot: Bot,
    *,
    admin_telegram_id: int,
    only_user_id: int | None = None,
) -> str:
    report = await run_week_plan_batch(only_user_id=only_user_id)
    text = format_batch_summary(report)
    try:
        await bot.send_message(admin_telegram_id, text)
    except Exception:
        logger.exception("failed to DM week_plan preview")
    return text
