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


async def get_session() -> AsyncGenerator[AsyncSession, None]:
    async with SessionLocal() as session:
        yield session
