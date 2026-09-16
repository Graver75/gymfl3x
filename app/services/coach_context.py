"""Compact athlete context for the LLM coach (small CPU models)."""

from __future__ import annotations

from datetime import date, timedelta
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.services.athlete_features import build_athlete_snapshot

WINDOW_DAYS = {
    "session": 14,
    "week": 365,
    "week_plan": 365,
    "month": 45,
    "exercise": 45,
    "live_set": 45,
    "session_group": 14,
    "week_group": 365,
}
MAX_SESSIONS = {
    "session": 8,
    "week": 80,
    "week_plan": 80,
    "month": 18,
    "exercise": 14,
    "live_set": 14,
    "session_group": 8,
    "week_group": 80,
}
# Weekly kinds: last N sessions keep full by_ex; older ones are ultra-compact
WEEKLY_RECENT_SESSIONS = 16
WEEKLY_BW_POINTS = 52
WEEKLY_KINDS = frozenset({"week", "week_group", "week_plan"})
NOTE_MAX = 120


def _trim_note(text: str | None) -> str | None:
    if not text:
        return text
    t = text.strip()
    if len(t) <= NOTE_MAX:
        return t
    return t[: NOTE_MAX - 1] + "…"


def _compact_set(row: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {
        "ex": row.get("exercise_name"),
        "n": row.get("set_number"),
        "d": row.get("drop_index"),
        "reps": row.get("reps"),
        "kg": row.get("weight"),
        "diff": row.get("difficulty"),
        "rpe": row.get("rpe_1_10"),
    }
    machine = row.get("machine_name")
    if machine:
        out["m"] = machine
    if row.get("planned_kg") is not None:
        out["pkg"] = row.get("planned_kg")
    if row.get("planned_reps") is not None:
        out["preps"] = row.get("planned_reps")
    if row.get("planned_rpe") is not None:
        out["prpe"] = row.get("planned_rpe")
    if row.get("plan_source"):
        out["psrc"] = row.get("plan_source")
    if row.get("followed_kg") is not None:
        out["fkg"] = row.get("followed_kg")
    if row.get("followed_reps") is not None:
        out["freps"] = row.get("followed_reps")
    return out


def _session_volume(ws: dict[str, Any]) -> float:
    total = 0.0
    for s in ws.get("sets") or []:
        vol = s.get("volume")
        if vol is not None:
            total += float(vol)
        else:
            try:
                total += float(s.get("reps") or 0) * float(s.get("weight") or 0)
            except (TypeError, ValueError):
                pass
    return round(total, 1)


def _working_sets_n(sets: list[dict[str, Any]]) -> int:
    """Unique working sets (exercise + set_number), ignoring drop variants."""
    keys: set[tuple[Any, Any]] = set()
    for s in sets:
        keys.add((s.get("exercise_name"), s.get("set_number")))
    return len(keys)


def _ex_volume(vals: dict[str, Any]) -> float:
    total = 0.0
    for reps, kg in zip(vals.get("reps") or [], vals.get("kg") or []):
        try:
            total += float(reps or 0) * float(kg or 0)
        except (TypeError, ValueError):
            pass
    return total


def _sample_bw(series: list[dict[str, Any]], limit: int) -> list[dict[str, Any]]:
    if len(series) <= limit:
        return series
    if limit <= 2:
        return series[:1] + series[-1:]
    # Evenly sample including first and last
    n = len(series)
    idxs = {0, n - 1}
    for i in range(1, limit - 1):
        idxs.add(round(i * (n - 1) / (limit - 1)))
    return [series[i] for i in sorted(idxs)]


def _compact_session(
    ws: dict[str, Any],
    *,
    full_sets: bool,
    detail: str = "full",
) -> dict[str, Any]:
    """detail: full (by_ex all kg/reps) | older (ultra-compact for year history)."""
    sets = ws.get("sets") or []
    parts_n = len(sets)
    sets_n = _working_sets_n(sets)
    out: dict[str, Any] = {
        "id": ws.get("id"),
        "date": ws.get("date"),
        "tpl": ws.get("template_name"),
        "tpl_id": ws.get("template_id"),
        "vol": _session_volume(ws),
        "sets_n": sets_n,
        "parts_n": parts_n,
    }
    if detail == "older":
        # Ultra-compact: optional top-2 exercises by volume, no full rows
        by_ex: dict[str, dict[str, Any]] = {}
        for s in sets:
            name = s.get("exercise_name") or "?"
            slot = by_ex.setdefault(name, {"reps": [], "kg": []})
            if s.get("reps") is not None:
                slot["reps"].append(s["reps"])
            if s.get("weight") is not None:
                slot["kg"].append(s["weight"])
        ranked = sorted(by_ex.items(), key=lambda kv: -_ex_volume(kv[1]))[:2]
        if ranked:
            out["top"] = [
                {
                    "ex": name,
                    "vol": round(_ex_volume(vals), 1),
                    "parts": len(vals.get("reps") or []),
                }
                for name, vals in ranked
            ]
        return out

    out["dur"] = ws.get("duration_sec")
    out["checkin"] = ws.get("checkin")
    if full_sets:
        out["sets"] = [_compact_set(s) for s in sets]
        return out

    by_ex: dict[str, dict[str, Any]] = {}
    for s in sets:
        name = s.get("exercise_name") or "?"
        slot = by_ex.setdefault(
            name,
            {
                "reps": [],
                "kg": [],
                "diff": None,
                "rpe": [],
                "m": s.get("machine_name"),
                "set_keys": set(),
            },
        )
        if s.get("reps") is not None:
            slot["reps"].append(s["reps"])
        if s.get("weight") is not None:
            slot["kg"].append(s["weight"])
        if s.get("difficulty"):
            slot["diff"] = s["difficulty"]
        if s.get("rpe_1_10") is not None:
            slot["rpe"].append(s["rpe_1_10"])
        if s.get("machine_name") and not slot.get("m"):
            slot["m"] = s["machine_name"]
        slot["set_keys"].add(s.get("set_number"))
    out["by_ex"] = []
    for name, vals in by_ex.items():
        row: dict[str, Any] = {
            "ex": name,
            "sets": len(vals["set_keys"]),
            "parts": len(vals["reps"]),
            "reps": vals["reps"],
            "kg": vals["kg"],
            "diff": vals["diff"],
            "rpe_avg": (
                round(sum(vals["rpe"]) / len(vals["rpe"]), 1) if vals["rpe"] else None
            ),
        }
        if vals.get("m"):
            row["m"] = vals["m"]
        out["by_ex"].append(row)
    return out


def _avg_rpe(sessions: list[dict[str, Any]]) -> float | None:
    values: list[float] = []
    for ws in sessions:
        for s in ws.get("sets") or []:
            if s.get("rpe_1_10") is not None:
                values.append(float(s["rpe_1_10"]))
    if not values:
        return None
    return round(sum(values) / len(values), 2)


async def _week_schedule(session: AsyncSession) -> list[dict[str, Any]]:
    from app.services.reminders import get_template_for_weekday

    today = date.today()
    monday = today - timedelta(days=today.weekday())
    rows: list[dict[str, Any]] = []
    for offset in range(7):
        day = monday + timedelta(days=offset)
        tpl = await get_template_for_weekday(session, day.weekday())
        rows.append(
            {
                "wd": day.weekday(),
                "date": day.isoformat(),
                "tpl": tpl.name if tpl else None,
                "tpl_id": tpl.id if tpl else None,
            }
        )
    return rows


async def build_coach_context(
    session: AsyncSession,
    user_id: int,
    *,
    kind: str,
    focus_session_id: int | None = None,
    focus_exercise_id: int | None = None,
    focus_exercise_name: str | None = None,
    live: dict[str, Any] | None = None,
) -> dict[str, Any]:
    days = WINDOW_DAYS.get(kind, 21)
    snap = await build_athlete_snapshot(session, user_id, days=days)
    if snap.get("error"):
        return snap

    sessions = list(snap.get("sessions") or [])
    max_n = MAX_SESSIONS.get(kind, 12)

    focus_session = None
    if focus_session_id is not None:
        for ws in sessions:
            if ws.get("id") == focus_session_id:
                focus_session = ws
                break

    if kind == "session" and focus_session is not None:
        tpl_id = focus_session.get("template_id")
        same = [ws for ws in sessions if ws.get("template_id") == tpl_id]
        prev = [ws for ws in same if ws.get("id") != focus_session_id][-2:]
        picked = prev + [focus_session]
        other_ids = {ws.get("id") for ws in picked}
        extras = [ws for ws in sessions if ws.get("id") not in other_ids][-3:]
        sessions_out = [
            _compact_session(
                ws, full_sets=(ws.get("id") == focus_session_id or ws in prev)
            )
            for ws in extras + picked
        ]
    elif kind in {"exercise", "live_set"}:
        filtered = []
        for ws in sessions:
            sets = ws.get("sets") or []
            keep = []
            for s in sets:
                if focus_exercise_id is not None and s.get("exercise_id") == focus_exercise_id:
                    keep.append(s)
                elif focus_exercise_name and s.get("exercise_name") == focus_exercise_name:
                    keep.append(s)
            if keep:
                slim = dict(ws)
                slim["sets"] = keep
                filtered.append(slim)
        sessions_out = [
            _compact_session(ws, full_sets=True) for ws in filtered[-max_n:]
        ]
    elif kind in WEEKLY_KINDS:
        picked = sessions[-max_n:]
        recent_cut = max(0, len(picked) - WEEKLY_RECENT_SESSIONS)
        sessions_out = []
        for i, ws in enumerate(picked):
            if i < recent_cut:
                sessions_out.append(
                    _compact_session(ws, full_sets=False, detail="older")
                )
            else:
                sessions_out.append(
                    _compact_session(ws, full_sets=False, detail="full")
                )
    else:
        sessions_out = [
            _compact_session(ws, full_sets=False) for ws in sessions[-max_n:]
        ]

    bw_raw = list(snap.get("body_weight_series") or [])
    if kind in WEEKLY_KINDS:
        bw = _sample_bw(bw_raw, WEEKLY_BW_POINTS)
    else:
        bw = bw_raw[-10:]

    notes = []
    for n in snap.get("notes_timeline") or []:
        if kind in {"exercise", "live_set"}:
            if focus_exercise_id is not None and n.get("exercise_id") != focus_exercise_id:
                if not (focus_exercise_name and n.get("exercise_name") == focus_exercise_name):
                    continue
        notes.append(
            {
                "ex": n.get("exercise_name"),
                "text": _trim_note(n.get("text")),
                "cleared": n.get("cleared"),
                "at": n.get("created_at"),
            }
        )
    notes = notes[-15:] if kind not in WEEKLY_KINDS else notes[-40:]

    states = []
    for st in snap.get("exercise_state") or []:
        if kind in {"exercise", "live_set"}:
            if focus_exercise_id is not None and st.get("exercise_id") != focus_exercise_id:
                continue
        # month: prefer stuck / notable; weekly: all with weights
        if kind == "month" and not (st.get("hard_streak") or 0) and not st.get(
            "current_note"
        ):
            continue
        if kind in WEEKLY_KINDS:
            if not (
                st.get("working_weight")
                or st.get("suggested_weight")
                or st.get("hard_streak")
                or st.get("current_note")
            ):
                continue
        states.append(
            {
                "ex_id": st.get("exercise_id"),
                "ex": st.get("exercise_name"),
                **({"m": st["machine_name"]} if st.get("machine_name") else {}),
                "ww": st.get("working_weight"),
                "sw": st.get("suggested_weight"),
                "last_reps": st.get("last_reps"),
                "last_sets": st.get("last_sets"),
                "diff": st.get("last_difficulty"),
                "hard_streak": st.get("hard_streak"),
                "note": _trim_note(st.get("current_note")),
            }
        )
    if kind == "month" and len(states) > 20:
        states = sorted(states, key=lambda x: -(x.get("hard_streak") or 0))[:20]
    elif kind in WEEKLY_KINDS and len(states) > 60:
        states = sorted(
            states,
            key=lambda x: (
                -(x.get("hard_streak") or 0),
                -(float(x.get("ww") or x.get("sw") or 0)),
            ),
        )[:60]

    user = snap.get("user") or {}
    focus: dict[str, Any] = {
        "session_id": focus_session_id,
        "exercise_id": focus_exercise_id,
        "exercise_name": focus_exercise_name,
    }
    live_machine = (live or {}).get("machine_name") if live else None
    if live_machine:
        focus["machine_name"] = live_machine
    elif focus_exercise_id is not None:
        for st in snap.get("exercise_state") or []:
            if st.get("exercise_id") == focus_exercise_id and st.get("machine_name"):
                focus["machine_name"] = st["machine_name"]
                break
    elif focus_exercise_name:
        for st in snap.get("exercise_state") or []:
            if st.get("exercise_name") == focus_exercise_name and st.get("machine_name"):
                focus["machine_name"] = st["machine_name"]
                break

    from app.services.progression import PHASE_LABELS
    from app.db.models import TrainingPhase

    raw_phase = user.get("phase")
    try:
        phase_ru = PHASE_LABELS[TrainingPhase(raw_phase)] if raw_phase else None
    except (ValueError, KeyError):
        phase_ru = str(raw_phase) if raw_phase else None

    user_out: dict[str, Any] = {
        "phase": phase_ru,
        "log_level": user.get("log_level"),
        "bw": user.get("body_weight"),
        "exp_m": user.get("experience_months"),
        "code": user.get("short_code"),
        "sex": user.get("sex"),
        "age": user.get("age"),
    }
    if kind != "live_set" and user.get("height_cm") is not None:
        user_out["height_cm"] = user.get("height_cm")

    out: dict[str, Any] = {
        "kind": kind,
        "window_days": days,
        "user": user_out,
        "notes": notes,
        "exercise_state": states,
        "sessions": sessions_out,
        "focus": focus,
    }
    if kind != "live_set":
        out["adherence"] = snap.get("adherence")
        out["aggregates"] = {
            "sessions_n": len(snap.get("sessions") or []),
            "total_vol": round(
                sum(_session_volume(ws) for ws in snap.get("sessions") or []), 1
            ),
            "avg_rpe": _avg_rpe(snap.get("sessions") or []),
            "bw_delta": (
                round(float(bw[-1]["weight"]) - float(bw[0]["weight"]), 2)
                if len(bw) >= 2
                else None
            ),
        }
        out["body_weight_series"] = bw
        pa = snap.get("plan_adherence")
        if pa and (
            pa.get("with_plan_sets")
            or pa.get("exercises")
            or pa.get("deviations")
        ):
            # Compact for tokens
            out["plan_adherence"] = {
                "with_plan_sets": pa.get("with_plan_sets"),
                "followed_sets": pa.get("followed_sets"),
                "deviated_sets": pa.get("deviated_sets"),
                "exercises": (pa.get("exercises") or [])[:30],
                "deviations": (pa.get("deviations") or [])[-20:],
            }
        wps = snap.get("week_plans") or []
        if wps and kind in WEEKLY_KINDS | {"exercise", "month", "session"}:
            out["week_plans"] = [
                {
                    "ex_id": wp.get("exercise_id"),
                    "ex": wp.get("exercise_name"),
                    "sets": wp.get("sets"),
                    "advice": wp.get("advice"),
                }
                for wp in wps[:40]
            ]
    if kind in WEEKLY_KINDS:
        out["schedule"] = await _week_schedule(session)
    if live:
        out["live"] = live
    return out
