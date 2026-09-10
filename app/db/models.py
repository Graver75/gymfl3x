from __future__ import annotations

import enum
from datetime import date, datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    Date,
    DateTime,
    Enum,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


class TrainingPhase(str, enum.Enum):
    honeymoon = "honeymoon"
    intermediate = "intermediate"
    plateau = "plateau"


class Difficulty(str, enum.Enum):
    easy = "easy"
    normal = "normal"
    hard = "hard"
    failure = "failure"


class SessionStatus(str, enum.Enum):
    active = "active"
    finished = "finished"
    skipped = "skipped"


class LogLevel(str, enum.Enum):
    minimal = "minimal"
    standard = "standard"
    detailed = "detailed"


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    telegram_id: Mapped[int] = mapped_column(BigInteger, unique=True, index=True)
    display_name: Mapped[str] = mapped_column(String(64))
    short_code: Mapped[str] = mapped_column(String(8))
    body_weight: Mapped[float | None] = mapped_column(Float, nullable=True)
    height_cm: Mapped[float | None] = mapped_column(Float, nullable=True)
    experience_months: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # When experience_months was last set; effective stazh grows from this date.
    experience_as_of: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    phase: Mapped[TrainingPhase] = mapped_column(
        Enum(TrainingPhase),
        default=TrainingPhase.honeymoon,
    )
    log_level: Mapped[LogLevel] = mapped_column(
        Enum(LogLevel),
        default=LogLevel.minimal,
    )
    is_admin: Mapped[bool] = mapped_column(Boolean, default=False)
    is_program_admin: Mapped[bool] = mapped_column(Boolean, default=False)
    sex: Mapped[str | None] = mapped_column(String(16), nullable=True)  # male | female
    onboarding_done: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    exercise_states: Mapped[list[UserExerciseState]] = relationship(back_populates="user")
    sessions: Mapped[list[WorkoutSession]] = relationship(back_populates="user")
    body_weight_logs: Mapped[list[BodyWeightLog]] = relationship(back_populates="user")
    note_logs: Mapped[list[ExerciseNoteLog]] = relationship(back_populates="user")
    nn_dialog_messages: Mapped[list["NnDialogMessage"]] = relationship(back_populates="user")


class GroupChat(Base):
    __tablename__ = "group_chat"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    chat_id: Mapped[int] = mapped_column(BigInteger, unique=True)
    title: Mapped[str | None] = mapped_column(String(255), nullable=True)
    reminder_hour: Mapped[int] = mapped_column(Integer, default=8)
    recap_hour: Mapped[int] = mapped_column(Integer, default=22)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class WorkoutTemplate(Base):
    __tablename__ = "templates"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(128))
    hashtag: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    exercises: Mapped[list[TemplateExercise]] = relationship(
        back_populates="template",
        order_by="TemplateExercise.position",
        cascade="all, delete-orphan",
    )
    schedule_days: Mapped[list[ScheduleDay]] = relationship(back_populates="template")


class TemplateExercise(Base):
    __tablename__ = "template_exercises"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    template_id: Mapped[int] = mapped_column(ForeignKey("templates.id", ondelete="CASCADE"))
    name: Mapped[str] = mapped_column(String(128))
    position: Mapped[int] = mapped_column(Integer, default=0)
    target_sets: Mapped[int] = mapped_column(Integer, default=3)
    target_reps_min: Mapped[int] = mapped_column(Integer, default=8)
    target_reps_max: Mapped[int] = mapped_column(Integer, default=12)
    weight_step: Mapped[float] = mapped_column(Float, default=2.5)

    template: Mapped[WorkoutTemplate] = relationship(back_populates="exercises")
    user_states: Mapped[list[UserExerciseState]] = relationship(back_populates="exercise")


class ScheduleDay(Base):
    __tablename__ = "schedule"
    __table_args__ = (UniqueConstraint("weekday", name="uq_schedule_weekday"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    weekday: Mapped[int] = mapped_column(Integer)  # 0=Mon .. 6=Sun
    template_id: Mapped[int] = mapped_column(ForeignKey("templates.id", ondelete="CASCADE"))

    template: Mapped[WorkoutTemplate] = relationship(back_populates="schedule_days")


class UserExerciseState(Base):
    __tablename__ = "user_exercise_state"
    __table_args__ = (
        UniqueConstraint("user_id", "exercise_id", name="uq_user_exercise"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    exercise_id: Mapped[int] = mapped_column(ForeignKey("template_exercises.id", ondelete="CASCADE"))
    working_weight: Mapped[float | None] = mapped_column(Float, nullable=True)
    suggested_weight: Mapped[float | None] = mapped_column(Float, nullable=True)
    last_reps: Mapped[int | None] = mapped_column(Integer, nullable=True)
    last_sets: Mapped[int | None] = mapped_column(Integer, nullable=True)
    last_difficulty: Mapped[Difficulty | None] = mapped_column(Enum(Difficulty), nullable=True)
    hard_streak: Mapped[int] = mapped_column(Integer, default=0)
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
    )

    user: Mapped[User] = relationship(back_populates="exercise_states")
    exercise: Mapped[TemplateExercise] = relationship(back_populates="user_states")


class WorkoutSession(Base):
    __tablename__ = "sessions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    template_id: Mapped[int] = mapped_column(ForeignKey("templates.id", ondelete="SET NULL"), nullable=True)
    session_date: Mapped[date] = mapped_column(Date, index=True)
    status: Mapped[SessionStatus] = mapped_column(Enum(SessionStatus), default=SessionStatus.active)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    energy_1_5: Mapped[int | None] = mapped_column(Integer, nullable=True)
    sleep_1_5: Mapped[int | None] = mapped_column(Integer, nullable=True)
    pain_1_5: Mapped[int | None] = mapped_column(Integer, nullable=True)

    user: Mapped[User] = relationship(back_populates="sessions")
    template: Mapped[WorkoutTemplate | None] = relationship()
    sets: Mapped[list[SessionSet]] = relationship(
        back_populates="session",
        cascade="all, delete-orphan",
        order_by="SessionSet.id",
    )


class SessionSet(Base):
    __tablename__ = "session_sets"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    session_id: Mapped[int] = mapped_column(ForeignKey("sessions.id", ondelete="CASCADE"))
    exercise_id: Mapped[int] = mapped_column(ForeignKey("template_exercises.id", ondelete="SET NULL"), nullable=True)
    exercise_name: Mapped[str] = mapped_column(String(128))
    reps: Mapped[int] = mapped_column(Integer)
    weight: Mapped[float] = mapped_column(Float)
    sets_count: Mapped[int] = mapped_column(Integer)
    difficulty: Mapped[Difficulty] = mapped_column(Enum(Difficulty))
    volume: Mapped[float] = mapped_column(Float)  # reps * weight * sets
    set_number: Mapped[int] = mapped_column(Integer, default=0)
    drop_index: Mapped[int] = mapped_column(Integer, default=0)
    rpe_1_10: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    session: Mapped[WorkoutSession] = relationship(back_populates="sets")
    exercise: Mapped[TemplateExercise | None] = relationship()


class BodyWeightLog(Base):
    __tablename__ = "body_weight_logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    weight: Mapped[float] = mapped_column(Float)
    recorded_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    user: Mapped[User] = relationship(back_populates="body_weight_logs")


class ExerciseNoteLog(Base):
    __tablename__ = "exercise_note_logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    exercise_id: Mapped[int | None] = mapped_column(
        ForeignKey("template_exercises.id", ondelete="SET NULL"), nullable=True
    )
    exercise_name: Mapped[str] = mapped_column(String(128))
    session_id: Mapped[int | None] = mapped_column(
        ForeignKey("sessions.id", ondelete="SET NULL"), nullable=True
    )
    text: Mapped[str | None] = mapped_column(Text, nullable=True)
    cleared: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    user: Mapped[User] = relationship(back_populates="note_logs")


class ExerciseArchive(Base):
    """Named exercises that survive removal from templates."""

    __tablename__ = "exercise_archive"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(128))
    name_key: Mapped[str] = mapped_column(String(128), unique=True, index=True)
    target_sets: Mapped[int] = mapped_column(Integer, default=3)
    target_reps_min: Mapped[int] = mapped_column(Integer, default=8)
    target_reps_max: Mapped[int] = mapped_column(Integer, default=12)
    weight_step: Mapped[float] = mapped_column(Float, default=2.5)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
    )


class ExerciseStrengthStandard(Base):
    """Bodyweight (or reps) thresholds for 10 strength levels per exercise."""

    __tablename__ = "exercise_strength_standard"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(128))
    name_key: Mapped[str] = mapped_column(String(128), unique=True, index=True)
    mode: Mapped[str] = mapped_column(String(16), default="ratio")  # ratio | reps
    male_t1: Mapped[float] = mapped_column(Float, default=0.15)
    male_t2: Mapped[float] = mapped_column(Float, default=0.25)
    male_t3: Mapped[float] = mapped_column(Float, default=0.35)
    male_t4: Mapped[float] = mapped_column(Float, default=0.45)
    male_t5: Mapped[float] = mapped_column(Float, default=0.55)
    male_t6: Mapped[float] = mapped_column(Float, default=0.7)
    male_t7: Mapped[float] = mapped_column(Float, default=0.85)
    male_t8: Mapped[float] = mapped_column(Float, default=1.0)
    male_t9: Mapped[float] = mapped_column(Float, default=1.2)
    male_t10: Mapped[float] = mapped_column(Float, default=1.4)
    female_t1: Mapped[float] = mapped_column(Float, default=0.1)
    female_t2: Mapped[float] = mapped_column(Float, default=0.18)
    female_t3: Mapped[float] = mapped_column(Float, default=0.25)
    female_t4: Mapped[float] = mapped_column(Float, default=0.35)
    female_t5: Mapped[float] = mapped_column(Float, default=0.45)
    female_t6: Mapped[float] = mapped_column(Float, default=0.55)
    female_t7: Mapped[float] = mapped_column(Float, default=0.7)
    female_t8: Mapped[float] = mapped_column(Float, default=0.85)
    female_t9: Mapped[float] = mapped_column(Float, default=1.0)
    female_t10: Mapped[float] = mapped_column(Float, default=1.2)
    needs_review: Mapped[bool] = mapped_column(Boolean, default=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
    )


class RecapSent(Base):
    """Tracks whether morning reminder / evening recap was already sent for a date."""

    __tablename__ = "recap_sent"
    __table_args__ = (
        UniqueConstraint("chat_id", "recap_date", "kind", name="uq_recap_day_kind"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    chat_id: Mapped[int] = mapped_column(BigInteger)
    recap_date: Mapped[date] = mapped_column(Date)
    kind: Mapped[str] = mapped_column(String(16))  # reminder | recap
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class NnDialogMessage(Base):
    """Per-user LLM coach dialogue (one private thread; system prompt is not stored)."""

    __tablename__ = "nn_dialog_messages"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    role: Mapped[str] = mapped_column(String(16))  # user | assistant
    content: Mapped[str] = mapped_column(Text)
    kind: Mapped[str | None] = mapped_column(String(32), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    user: Mapped[User] = relationship(back_populates="nn_dialog_messages")
