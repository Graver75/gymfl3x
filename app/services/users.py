from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession
from zoneinfo import ZoneInfo

from app.config import get_settings
from app.db.models import (
    BodyWeightLog,
    ExerciseNoteLog,
    NnDialogMessage,
    User,
    UserExerciseState,
    WorkoutSession,
)
from app.services.metrics_log import log_body_weight
from app.services.progression import phase_from_experience


def can_open_admin(user: User) -> bool:
    return bool(user.is_admin or getattr(user, "is_program_admin", False))


def _now_tz() -> datetime:
    settings = get_settings()
    try:
        return datetime.now(ZoneInfo(settings.timezone))
    except Exception:
        return datetime.now(timezone.utc)


def _as_aware(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


def calendar_months_elapsed(since: datetime, *, now: datetime | None = None) -> int:
    """Full calendar months from since → now (never negative)."""
    end = _as_aware(now or _now_tz())
    start = _as_aware(since)
    months = (end.year - start.year) * 12 + (end.month - start.month)
    if end.day < start.day:
        months -= 1
    return max(0, months)


def effective_experience_months(user: User, *, now: datetime | None = None) -> int | None:
    """Stazh in months: stored base + months since experience_as_of (or created_at)."""
    if user.experience_months is None:
        return None
    base = int(user.experience_months)
    as_of = getattr(user, "experience_as_of", None) or user.created_at
    if as_of is None:
        return base
    return base + calendar_months_elapsed(as_of, now=now)


def set_experience_months(user: User, months: int, *, now: datetime | None = None) -> None:
    """Record current total stazh; growth continues from this moment."""
    user.experience_months = int(months)
    user.experience_as_of = now or _now_tz()


async def get_or_create_user(
    session: AsyncSession,
    telegram_id: int,
    full_name: str,
) -> User:
    result = await session.execute(select(User).where(User.telegram_id == telegram_id))
    user = result.scalar_one_or_none()
    settings = get_settings()
    is_admin = telegram_id in settings.admin_telegram_ids

    if user is None:
        user = User(
            telegram_id=telegram_id,
            display_name=full_name[:64],
            short_code=_default_code(full_name),
            is_admin=is_admin,
        )
        session.add(user)
        await session.commit()
        await session.refresh(user)
        return user

    changed = False
    if is_admin and not user.is_admin:
        user.is_admin = True
        changed = True
    if changed:
        await session.commit()
        await session.refresh(user)
    return user


def _default_code(name: str) -> str:
    cleaned = "".join(ch for ch in name if ch.isalpha())
    return (cleaned[:1] or "?").upper()


async def apply_onboarding(
    session: AsyncSession,
    user: User,
    *,
    display_name: str,
    short_code: str,
    body_weight: float,
    height_cm: float | None,
    experience_months: int,
) -> User:
    user.display_name = display_name[:64]
    user.short_code = short_code[:8].upper()
    user.body_weight = body_weight
    user.height_cm = height_cm
    set_experience_months(user, experience_months)
    user.phase = phase_from_experience(experience_months)
    user.onboarding_done = True
    await log_body_weight(session, user.id, body_weight)
    await session.commit()
    await session.refresh(user)
    return user


async def reset_own_training_data(session: AsyncSession, user_id: int) -> dict[str, int]:
    """Wipe personal training/NN data. Keeps profile identity, roles, settings."""
    # Note logs first (FK to sessions)
    notes = await session.execute(
        delete(ExerciseNoteLog).where(ExerciseNoteLog.user_id == user_id)
    )
    # Sessions cascade to session_sets
    sessions = await session.execute(
        delete(WorkoutSession).where(WorkoutSession.user_id == user_id)
    )
    states = await session.execute(
        delete(UserExerciseState).where(UserExerciseState.user_id == user_id)
    )
    bw = await session.execute(
        delete(BodyWeightLog).where(BodyWeightLog.user_id == user_id)
    )
    dialog = await session.execute(
        delete(NnDialogMessage).where(NnDialogMessage.user_id == user_id)
    )
    await session.commit()
    return {
        "sessions": sessions.rowcount or 0,
        "exercise_states": states.rowcount or 0,
        "body_weight_logs": bw.rowcount or 0,
        "note_logs": notes.rowcount or 0,
        "nn_dialog": dialog.rowcount or 0,
    }
