"""Helpers to persist body-weight and exercise-note history for NN export."""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import BodyWeightLog, ExerciseNoteLog


async def log_body_weight(session: AsyncSession, user_id: int, weight: float) -> None:
    session.add(BodyWeightLog(user_id=user_id, weight=float(weight)))


async def log_exercise_note(
    session: AsyncSession,
    *,
    user_id: int,
    exercise_name: str,
    text: str | None,
    cleared: bool = False,
    exercise_id: int | None = None,
    session_id: int | None = None,
) -> None:
    session.add(
        ExerciseNoteLog(
            user_id=user_id,
            exercise_id=exercise_id,
            exercise_name=exercise_name[:128],
            session_id=session_id,
            text=text,
            cleared=cleared,
        )
    )
