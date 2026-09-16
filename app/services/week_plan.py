"""Hidden AI job: weekly per-exercise set prescriptions for athletes."""

from __future__ import annotations

import asyncio
import json
import logging
import re
from datetime import date, datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

from aiogram import Bot
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app import ui_copy as ui
from app.config import get_settings
from app.db.models import (
    AppSetting,
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
    "program_review": "Разбор программы",
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
            pass
    salvaged = _salvage_exercises(cleaned)
    if salvaged:
        return {"exercises": salvaged}
    return None


def _salvage_exercises(raw: str) -> list[dict[str, Any]]:
    """Pull fully-formed exercise objects out of a truncated week_plan JSON."""
    out: list[dict[str, Any]] = []
    for m in re.finditer(
        r'\{\s*"exercise_id"\s*:\s*\d+\s*,[\s\S]*?\}\s*(?=,|\]|$)',
        raw,
    ):
        chunk = m.group(0).rstrip().rstrip(",")
        if chunk.count("{") != chunk.count("}"):
            continue
        try:
            obj = json.loads(chunk)
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict) and "exercise_id" in obj:
            out.append(obj)
    return out


EXERCISE_CHUNK = 5
WEEK_PLAN_MAX_TOKENS = 4096
SETTING_LAST_PREVIEW = "hidden_week_plan_last_preview"
SETTING_RUN_HISTORY = "hidden_week_plan_run_history"
PREVIEW_CHUNK = 3500
HISTORY_MAX = 20

# In-process run flag (single systemd worker)
_job_lock = asyncio.Lock()
_job_running: dict[str, Any] | None = None


def is_week_plan_running() -> bool:
    return _job_running is not None


def get_week_plan_run_status() -> dict[str, Any] | None:
    """Snapshot of current run, or None if idle."""
    if _job_running is None:
        return None
    return dict(_job_running)


async def load_run_history(session: AsyncSession) -> list[dict[str, Any]]:
    row = await session.get(AppSetting, SETTING_RUN_HISTORY)
    if not row or not (row.value or "").strip():
        return []
    try:
        data = json.loads(row.value)
    except json.JSONDecodeError:
        return []
    return data if isinstance(data, list) else []


async def append_run_history(session: AsyncSession, entry: dict[str, Any]) -> None:
    hist = await load_run_history(session)
    hist.insert(0, entry)
    hist = hist[:HISTORY_MAX]
    value = json.dumps(hist, ensure_ascii=False)
    row = await session.get(AppSetting, SETTING_RUN_HISTORY)
    if row is None:
        session.add(AppSetting(key=SETTING_RUN_HISTORY, value=value))
    else:
        row.value = value
    await session.commit()


def format_hidden_status_html(*, history: list[dict[str, Any]] | None = None) -> str:
    """Status block for admin hidden-AI screen."""
    from app.services.program_review import get_program_review_run_status

    run = get_week_plan_run_status() or get_program_review_run_status()
    lines = [ui.b(ui.BTN_ADM_HIDDEN_AI), ""]
    if run:
        started = str(run.get("started_at") or "?")
        if "T" in started:
            started = started.replace("T", " ")[:19]
        scope = run.get("scope") or "?"
        lines.append("Статус: <b>🟢 выполняется</b>")
        lines.append(f"с {ui.esc(started)} · {ui.esc(scope)}")
    else:
        lines.append("Статус: <b>⚪ не выполняется</b>")
    lines.append("")
    lines.append(
        "Скрытые job'ы не пишут в чат атлетам — результат в БД / карточке.\n"
        "После форса полный результат придёт тебе в личку.\n"
        "Автозапуск: вместе с недельным дайджестом (тот же день/час).\n"
        "Разбор программы — только если fingerprint изменился."
    )
    hist = history or []
    if hist:
        lines.append("")
        lines.append("<b>История</b> (последние):")
        for h in hist[:10]:
            at = str(h.get("at") or "?")
            if "T" in at:
                at = at.replace("T", " ")[5:16]
            mark = "✓" if h.get("ok") else "✗"
            if h.get("skipped"):
                mark = "⏭"
            scope = h.get("scope") or "?"
            job = h.get("job") or "week_plan"
            if job == "program_review":
                extra = h.get("reason") or h.get("fingerprint") or ""
                lines.append(
                    f"· <code>{ui.esc(at)}</code> {mark} программа · "
                    f"{ui.esc(scope)} · {ui.esc(str(extra)[:40])}"
                )
            else:
                ok_a = h.get("ok_athletes", "?")
                n_a = h.get("athletes", "?")
                saved = h.get("saved", "?")
                lines.append(
                    f"· <code>{ui.esc(at)}</code> {mark} {ui.esc(scope)} · "
                    f"{ok_a}/{n_a} атл. · +{saved} упр."
                )
    else:
        lines.append("")
        lines.append("<i>История пока пуста.</i>")
    return "\n".join(lines)



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


def _target_exercises_payload(
    exercises: list[TemplateExercise],
    *,
    machines: dict[str, str | None] | None = None,
) -> list[dict[str, Any]]:
    from app.services.archive import name_key

    rows: list[dict[str, Any]] = []
    for ex in exercises:
        machine = getattr(ex, "machine_name", None)
        if not machine and machines is not None:
            machine = machines.get(name_key(ex.name))
        rows.append(
            {
                "exercise_id": ex.id,
                "name": ex.name,
                "machine_name": machine,
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
    """Call LLM for one athlete (chunked) and upsert plans. Returns summary dict."""
    exs = exercises if exercises is not None else await exercises_for_week(session, week_start)
    if not exs:
        return {
            "user_id": user.id,
            "code": user.short_code,
            "ok": False,
            "error": "no_exercises",
            "saved": 0,
        }

    ctx = await build_coach_context(session, user.id, kind="week_plan")
    allowed = {ex.id for ex in exs}
    saved_total = 0
    raw_parts: list[str] = []
    errors: list[str] = []
    n_chunks = (len(exs) + EXERCISE_CHUNK - 1) // EXERCISE_CHUNK

    from app.services.archive import name_key, resolve_machine_for_name

    machines: dict[str, str | None] = {}
    for ex in exs:
        key = name_key(ex.name)
        if key in machines:
            continue
        machines[key] = (ex.machine_name or None) or await resolve_machine_for_name(
            session, ex.name
        )

    for i in range(0, len(exs), EXERCISE_CHUNK):
        chunk = exs[i : i + EXERCISE_CHUNK]
        chunk_i = i // EXERCISE_CHUNK + 1
        payload = {
            **ctx,
            "kind": "week_plan",
            "week_start": week_start.isoformat(),
            "target_exercises": _target_exercises_payload(chunk, machines=machines),
            "chunk": f"{chunk_i}/{n_chunks}",
        }
        raw = await request_coach(
            kind="week_plan",
            athlete=payload,
            focus={"week_start": week_start.isoformat()},
            history=[],
            user_id=user.id,
            user_label=user.short_code,
            max_tokens=WEEK_PLAN_MAX_TOKENS,
        )
        if not raw:
            errors.append(f"chunk{chunk_i}:llm_empty")
            continue
        raw_parts.append(raw)
        parsed = extract_json_object(raw)
        if not parsed:
            errors.append(f"chunk{chunk_i}:json_parse")
            continue
        items = parsed.get("exercises")
        if not isinstance(items, list):
            errors.append(f"chunk{chunk_i}:bad_schema")
            continue
        chunk_saved = 0
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
            chunk_saved += 1
            saved_total += 1
        # Release write locks between LLM chunks (SQLite + concurrent live_set)
        if chunk_saved:
            await session.commit()

    full_raw = "\n\n---\n\n".join(raw_parts)
    if saved_total > 0:
        return {
            "user_id": user.id,
            "code": user.short_code,
            "ok": True,
            "saved": saved_total,
            "raw_preview": full_raw,
            "warnings": errors or None,
        }
    err = "llm_empty"
    if errors:
        if all(e.endswith("llm_empty") for e in errors):
            err = "llm_empty"
        elif any(e.endswith("json_parse") for e in errors):
            err = "json_parse"
        else:
            err = errors[0]
    return {
        "user_id": user.id,
        "code": user.short_code,
        "ok": False,
        "error": err,
        "saved": 0,
        "raw_preview": full_raw or None,
        "warnings": errors or None,
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
        user_ids = [u.id for u in users]
        target_n = len(exercises)

    results: list[dict[str, Any]] = []
    for uid in user_ids:
        async with SessionLocal() as session:
            user = await session.get(User, uid)
            if not user:
                continue
            try:
                exs = await exercises_for_week(session, ws)
                res = await run_week_plan_for_user(
                    session, user, week_start=ws, exercises=exs
                )
                results.append(res)
            except Exception as exc:
                logger.exception("week_plan failed user=%s", uid)
                results.append(
                    {
                        "user_id": uid,
                        "code": user.short_code if user else None,
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
        "target_exercise_n": target_n,
        "results": results,
    }


def format_batch_summary(report: dict[str, Any], *, max_preview: int = 800) -> str:
    if report.get("error") == "nn_offline":
        return "ИИ офлайн — week_plan не запущен"
    if report.get("error") == "no_exercises":
        return f"Нет упражнений в расписании на неделю {report.get('week_start')}"

    lines = [
        f"Прогноз недели · week_start={report.get('week_start')}",
        f"Атлетов: {report.get('ok_athletes', 0)}/{report.get('athletes', 0)} ок",
        f"Упражнений записано: {report.get('exercises_saved', 0)} "
        f"(целевых в шаблонах: {report.get('target_exercise_n', 0)})",
        "",
        "Полный raw — кнопками «📜 Превью» ниже (не обрезается).",
    ]
    for r in report.get("results") or []:
        code = r.get("code") or r.get("user_id")
        if r.get("ok"):
            warn = r.get("warnings")
            extra = f" (warn: {', '.join(warn)})" if warn else ""
            lines.append(f"· {code}: +{r.get('saved')} упр.{extra}")
        else:
            lines.append(f"· {code}: FAIL ({r.get('error')})")
            if r.get("raw_preview"):
                lines.append("  → есть raw, раскрой превью")
    text = "\n".join(lines)
    if len(text) > 3500:
        text = text[:3490] + "…"
    return text


async def save_last_force_preview(session: AsyncSession, report: dict[str, Any]) -> None:
    """Persist per-athlete raw for expand buttons in admin."""
    payload = {
        "week_start": report.get("week_start"),
        "athletes": [
            {
                "user_id": r.get("user_id"),
                "code": r.get("code"),
                "ok": bool(r.get("ok")),
                "error": r.get("error"),
                "saved": r.get("saved"),
                "raw": r.get("raw_preview") or "",
            }
            for r in (report.get("results") or [])
            if r.get("user_id") is not None
        ],
    }
    value = json.dumps(payload, ensure_ascii=False)
    # Cap stored blob to ~200k chars
    if len(value) > 200_000:
        for a in payload["athletes"]:
            a["raw"] = (a.get("raw") or "")[:20_000]
        value = json.dumps(payload, ensure_ascii=False)
    row = await session.get(AppSetting, SETTING_LAST_PREVIEW)
    if row is None:
        session.add(AppSetting(key=SETTING_LAST_PREVIEW, value=value))
    else:
        row.value = value
    await session.commit()


async def load_last_force_preview(session: AsyncSession) -> dict[str, Any] | None:
    row = await session.get(AppSetting, SETTING_LAST_PREVIEW)
    if not row or not (row.value or "").strip():
        return None
    try:
        data = json.loads(row.value)
    except json.JSONDecodeError:
        return None
    return data if isinstance(data, dict) else None


def preview_athlete_raw(store: dict[str, Any], user_id: int) -> tuple[str, str]:
    """Returns (label, raw_text)."""
    for a in store.get("athletes") or []:
        if int(a.get("user_id") or 0) == int(user_id):
            code = a.get("code") or str(user_id)
            status = "OK" if a.get("ok") else f"FAIL ({a.get('error')})"
            raw = a.get("raw") or "—"
            return f"{code} · {status}", str(raw)
    return str(user_id), "Нет сохранённого превью"

def _chunk_text(text: str, limit: int = 3500) -> list[str]:
    text = (text or "").strip()
    if not text:
        return []
    if len(text) <= limit:
        return [text]
    chunks: list[str] = []
    buf: list[str] = []
    size = 0
    for line in text.split("\n"):
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


async def format_saved_plans_detail(
    session: AsyncSession,
    *,
    week_start: date,
    user_ids: list[int] | None = None,
) -> str:
    """Human-readable plans from DB after a run."""
    q = (
        select(UserExerciseWeekPlan, User, TemplateExercise)
        .join(User, User.id == UserExerciseWeekPlan.user_id)
        .join(TemplateExercise, TemplateExercise.id == UserExerciseWeekPlan.exercise_id)
        .where(UserExerciseWeekPlan.week_start == week_start)
        .order_by(User.short_code, TemplateExercise.position, TemplateExercise.id)
    )
    if user_ids:
        q = q.where(UserExerciseWeekPlan.user_id.in_(user_ids))
    rows = list((await session.execute(q)).all())
    if not rows:
        return f"В БД нет планов на неделю с {week_start.isoformat()}."

    lines = [f"Результат планов · неделя с {week_start.isoformat()}", ""]
    current_code: str | None = None
    for plan, user, ex in rows:
        code = user.short_code or str(user.id)
        if code != current_code:
            if current_code is not None:
                lines.append("")
            lines.append(f"══ {code} ({user.display_name or '—'}) ══")
            current_code = code
        machine = f" [{ex.machine_name}]" if ex.machine_name else ""
        lines.append(f"• {ex.name}{machine}")
        for s in parse_sets_json(plan.sets_json):
            rpe = s.get("rpe")
            rpe_s = f" @RPE{rpe}" if rpe else ""
            lines.append(f"  {s['n']}: {s['kg']:g}×{s['reps']}{rpe_s}")
        advice = (plan.advice or "").strip()
        if advice:
            lines.append(f"  Совет: {advice}")
    return "\n".join(lines)


async def send_admin_result_messages(
    bot: Bot,
    admin_telegram_id: int,
    *parts: str,
) -> int:
    """Send one or more plain-text result messages. Returns count sent."""
    sent = 0
    for part in parts:
        for chunk in _chunk_text(part):
            try:
                await bot.send_message(admin_telegram_id, chunk)
                sent += 1
            except Exception:
                logger.exception("failed to DM week_plan chunk")
    return sent


async def maybe_run_scheduled_week_plan(bot: Bot | None = None) -> None:
    """Same hour/weekday as week digests; dedup via RecapSent chat_id=0."""
    global _job_running
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

    if _job_lock.locked() or _job_running is not None:
        logger.info("week_plan scheduled skipped — already running")
        return

    async with _job_lock:
        _job_running = {
            "scope": "cron",
            "started_at": datetime.now(timezone.utc).isoformat(),
        }
        report: dict[str, Any] = {}
        try:
            report = await run_week_plan_batch()
            async with SessionLocal() as session:
                await _mark_sent(session, 0, today, "ai_week_plan")
                await append_run_history(
                    session,
                    {
                        "at": datetime.now(timezone.utc).isoformat(),
                        "scope": "cron",
                        "ok": bool(report.get("ok")),
                        "athletes": report.get("athletes"),
                        "ok_athletes": report.get("ok_athletes"),
                        "saved": report.get("exercises_saved"),
                        "week_start": report.get("week_start"),
                        "error": report.get("error"),
                    },
                )
        finally:
            _job_running = None

    logger.info(
        "week_plan scheduled: ok=%s athletes=%s saved=%s",
        report.get("ok"),
        report.get("athletes"),
        report.get("exercises_saved"),
    )
    if bot is not None and report.get("results"):
        _ = bot


async def force_week_plan(
    bot: Bot,
    *,
    admin_telegram_id: int,
    only_user_id: int | None = None,
) -> tuple[str, dict[str, Any]]:
    """Run job and DM admin a summary + full readable plans from DB.

    Returns (panel_text, report) — report used for expand-preview buttons.
    """
    global _job_running
    if _job_running is not None or _job_lock.locked():
        run = get_week_plan_run_status() or {}
        started = str(run.get("started_at") or "?")
        return (
            f"Уже выполняется (с {started}, {run.get('scope')}). "
            "Дождись окончания или обнови статус.",
            {},
        )

    scope = f"me:{only_user_id}" if only_user_id is not None else "all"
    async with _job_lock:
        _job_running = {
            "scope": scope,
            "started_at": datetime.now(timezone.utc).isoformat(),
            "admin_telegram_id": admin_telegram_id,
        }
        report: dict[str, Any] = {}
        try:
            report = await run_week_plan_batch(only_user_id=only_user_id)
            async with SessionLocal() as session:
                await append_run_history(
                    session,
                    {
                        "at": datetime.now(timezone.utc).isoformat(),
                        "scope": scope,
                        "ok": bool(report.get("ok")),
                        "athletes": report.get("athletes"),
                        "ok_athletes": report.get("ok_athletes"),
                        "saved": report.get("exercises_saved"),
                        "week_start": report.get("week_start"),
                        "error": report.get("error"),
                    },
                )
        finally:
            _job_running = None

    if not report:
        return "Job оборвался без результата.", {}

    summary = format_batch_summary(report)

    week_raw = report.get("week_start")
    try:
        week_start = date.fromisoformat(str(week_raw)) if week_raw else target_week_start()
    except ValueError:
        week_start = target_week_start()

    user_ids: list[int] | None = None
    if only_user_id is not None:
        user_ids = [only_user_id]
    else:
        user_ids = [
            int(r["user_id"])
            for r in (report.get("results") or [])
            if r.get("user_id") is not None
        ] or None

    async with SessionLocal() as session:
        await save_last_force_preview(session, report)
        detail = await format_saved_plans_detail(
            session, week_start=week_start, user_ids=user_ids
        )

    raw_bits: list[str] = []
    for r in report.get("results") or []:
        preview = r.get("raw_preview")
        if not preview:
            continue
        code = r.get("code") or r.get("user_id")
        tag = "OK" if r.get("ok") else "FAIL"
        raw_bits.append(f"--- {code} [{tag}] raw ---\n{preview}")
    raw_block = "\n\n".join(raw_bits[:5])

    header = "🤫 Результат скрытого job: Прогноз недели"
    n = await send_admin_result_messages(
        bot,
        admin_telegram_id,
        f"{header}\n\n{summary}",
        detail,
    )
    if raw_block:
        for chunk in _chunk_text(raw_block):
            try:
                await bot.send_message(admin_telegram_id, ui.pre(chunk))
                n += 1
            except Exception:
                logger.exception("failed to DM week_plan raw chunk")
    panel = (
        f"{summary}\n\n"
        f"Полный результат отправлен в личку ({n} сообщ.).\n"
        "Кнопки «📜 Превью» — раскрыть raw без обрезки.\n"
        "Сырой request/response — также в «Запросы ИИ» (kind=week_plan)."
    )
    if len(panel) > 3500:
        panel = panel[:3490] + "…"
    return panel, report
