from collections.abc import AsyncGenerator

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.config import get_settings
from app.db.models import Base

settings = get_settings()
engine = create_async_engine(settings.database_url, echo=False)
SessionLocal = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)


async def init_db() -> None:
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        await _add_missing_columns(conn)


async def _add_missing_columns(conn) -> None:
    result = await conn.execute(text("PRAGMA table_info(session_sets)"))
    cols = {row[1] for row in result}
    if "set_number" not in cols:
        await conn.execute(text("ALTER TABLE session_sets ADD COLUMN set_number INTEGER DEFAULT 0"))
    if "drop_index" not in cols:
        await conn.execute(text("ALTER TABLE session_sets ADD COLUMN drop_index INTEGER DEFAULT 0"))

    result = await conn.execute(text("PRAGMA table_info(user_exercise_state)"))
    cols = {row[1] for row in result}
    if "note" not in cols:
        await conn.execute(text("ALTER TABLE user_exercise_state ADD COLUMN note TEXT"))

    result = await conn.execute(text("PRAGMA table_info(users)"))
    cols = {row[1] for row in result}
    if "is_program_admin" not in cols:
        await conn.execute(
            text("ALTER TABLE users ADD COLUMN is_program_admin BOOLEAN DEFAULT 0")
        )
    if "log_level" not in cols:
        await conn.execute(
            text("ALTER TABLE users ADD COLUMN log_level VARCHAR(16) DEFAULT 'minimal'")
        )
    if "sex" not in cols:
        await conn.execute(text("ALTER TABLE users ADD COLUMN sex VARCHAR(16)"))
    if "experience_as_of" not in cols:
        await conn.execute(
            text("ALTER TABLE users ADD COLUMN experience_as_of DATETIME")
        )
        # Backfill: grow from account creation for users who already set stazh
        await conn.execute(
            text(
                "UPDATE users SET experience_as_of = created_at "
                "WHERE experience_months IS NOT NULL AND experience_as_of IS NULL"
            )
        )

    result = await conn.execute(text("PRAGMA table_info(sessions)"))
    cols = {row[1] for row in result}
    if "energy_1_5" not in cols:
        await conn.execute(text("ALTER TABLE sessions ADD COLUMN energy_1_5 INTEGER"))
    if "sleep_1_5" not in cols:
        await conn.execute(text("ALTER TABLE sessions ADD COLUMN sleep_1_5 INTEGER"))
    if "pain_1_5" not in cols:
        await conn.execute(text("ALTER TABLE sessions ADD COLUMN pain_1_5 INTEGER"))

    result = await conn.execute(text("PRAGMA table_info(session_sets)"))
    cols = {row[1] for row in result}
    if "rpe_1_10" not in cols:
        await conn.execute(text("ALTER TABLE session_sets ADD COLUMN rpe_1_10 INTEGER"))

    # Strength standards: ensure t6..t10 exist (migration 5 → 10 levels)
    result = await conn.execute(text("PRAGMA table_info(exercise_strength_standard)"))
    std_cols = {row[1] for row in result}
    if std_cols:
        for col in (
            "male_t6",
            "male_t7",
            "male_t8",
            "male_t9",
            "male_t10",
            "female_t6",
            "female_t7",
            "female_t8",
            "female_t9",
            "female_t10",
        ):
            if col not in std_cols:
                await conn.execute(
                    text(f"ALTER TABLE exercise_strength_standard ADD COLUMN {col} FLOAT")
                )


async def get_session() -> AsyncGenerator[AsyncSession, None]:
    async with SessionLocal() as session:
        yield session
