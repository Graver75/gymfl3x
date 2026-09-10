"""Compact athlete context for the LLM coach (small CPU models)."""

from __future__ import annotations

from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.services.athlete_features import build_athlete_snapshot

WINDOW_DAYS = {
    "session": 14,
    "week": 21,
    "month": 45,
    "exercise": 45,
}
MAX_SESSIONS = {
    "session": 8,
    "week": 12,
    "month": 18,
    "exercise": 14,
}
NOTE_MAX = 120


def _trim_note(text: str | None) -> str | None:
    if not text:
        return text
    t = text.strip()
    if len(t) <= NOTE_MAX:
        return t
    return t[: NOTE_MAX - 1] + "…"


def _compact_set(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "ex": row.get("exercise_name"),
        "n": row.get("set_number"),
        "d": row.get("drop_index"),
        "reps": row.get("reps"),
        "kg": row.get("weight"),
        "diff": row.get("difficulty"),
        "rpe": row.get("rpe_1_10"),
        "rest": row.get("rest_sec"),
    }


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


def _compact_session(ws: dict[str, Any], *, full_sets: bool) -> dict[str, Any]:
    sets = ws.get("sets") or []
    out: dict[str, Any] = {
        "id": ws.get("id"),
        "date": ws.get("date"),
        "tpl": ws.get("template_name"),
        "tpl_id": ws.get("template_id"),
        "dur": ws.get("duration_sec"),
        "checkin": ws.get("checkin"),
        "vol": _session_volume(ws),
        "sets_n": len(sets),
    }
    if full_sets:
        out["sets"] = [_compact_set(s) for s in sets]
    else:
        # Aggregate by exercise for week/month
        by_ex: dict[str, dict[str, Any]] = {}
        for s in sets:
            name = s.get("exercise_name") or "?"
            slot = by_ex.setdefault(name, {"reps": [], "kg": [], "diff": None, "rpe": []})
            if s.get("reps") is not None:
                slot["reps"].append(s["reps"])
            if s.get("weight") is not None:
                slot["kg"].append(s["weight"])
            if s.get("difficulty"):
                slot["diff"] = s["difficulty"]
            if s.get("rpe_1_10") is not None:
                slot["rpe"].append(s["rpe_1_10"])
        out["by_ex"] = [
            {
                "ex": name,
                "reps": vals["reps"][-4:],
                "kg": vals["kg"][-4:],
                "diff": vals["diff"],
                "rpe_avg": round(sum(vals["rpe"]) / len(vals["rpe"]), 1) if vals["rpe"] else None,
            }
            for name, vals in by_ex.items()
        ]
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


async def build_coach_context(
    session: AsyncSession,
    user_id: int,
    *,
    kind: str,
    focus_session_id: int | None = None,
    focus_exercise_id: int | None = None,
    focus_exercise_name: str | None = None,
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
        # Keep focus + up to 2 previous same-template + recent others trimmed
        prev = [ws for ws in same if ws.get("id") != focus_session_id][-2:]
        picked = prev + [focus_session]
        # Also keep a few other recent for context
        other_ids = {ws.get("id") for ws in picked}
        extras = [ws for ws in sessions if ws.get("id") not in other_ids][-3:]
        sessions_out = [
            _compact_session(ws, full_sets=(ws.get("id") == focus_session_id or ws in prev))
            for ws in extras + picked
        ]
    elif kind == "exercise":
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
    else:
        sessions_out = [
            _compact_session(ws, full_sets=False) for ws in sessions[-max_n:]
        ]

    bw = list(snap.get("body_weight_series") or [])[-10:]
    notes = []
    for n in snap.get("notes_timeline") or []:
        if kind == "exercise":
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
    notes = notes[-15:]

    states = []
    for st in snap.get("exercise_state") or []:
        if kind == "exercise":
            if focus_exercise_id is not None and st.get("exercise_id") != focus_exercise_id:
                continue
        # Prefer stuck / notable
        if kind in {"week", "month"} and not (st.get("hard_streak") or 0) and not st.get("current_note"):
            continue
        states.append(
            {
                "ex_id": st.get("exercise_id"),
                "ww": st.get("working_weight"),
                "sw": st.get("suggested_weight"),
                "last_reps": st.get("last_reps"),
                "last_sets": st.get("last_sets"),
                "diff": st.get("last_difficulty"),
                "hard_streak": st.get("hard_streak"),
                "note": _trim_note(st.get("current_note")),
            }
        )
    if kind in {"week", "month"} and len(states) > 20:
        states = sorted(states, key=lambda x: -(x.get("hard_streak") or 0))[:20]

    user = snap.get("user") or {}
    return {
        "kind": kind,
        "window_days": days,
        "user": {
            "phase": user.get("phase"),
            "log_level": user.get("log_level"),
            "bw": user.get("body_weight"),
            "exp_m": user.get("experience_months"),
            "code": user.get("short_code"),
        },
        "adherence": snap.get("adherence"),
        "aggregates": {
            "sessions_n": len(snap.get("sessions") or []),
            "total_vol": round(sum(_session_volume(ws) for ws in snap.get("sessions") or []), 1),
            "avg_rpe": _avg_rpe(snap.get("sessions") or []),
            "bw_delta": (
                round(float(bw[-1]["weight"]) - float(bw[0]["weight"]), 2)
                if len(bw) >= 2
                else None
            ),
        },
        "body_weight_series": bw,
        "notes": notes,
        "exercise_state": states,
        "sessions": sessions_out,
        "focus": {
            "session_id": focus_session_id,
            "exercise_id": focus_exercise_id,
            "exercise_name": focus_exercise_name,
        },
    }
