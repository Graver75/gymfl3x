from collections.abc import AsyncGenerator

from sqlalchemy import event, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.config import get_settings
from app.db.models import Base

settings = get_settings()

_connect_args: dict = {}
if settings.database_url.startswith("sqlite"):
    # Wait up to 30s on lock instead of failing immediately (week_plan + live_set concurrent)
    _connect_args["timeout"] = 30

engine = create_async_engine(
    settings.database_url,
    echo=False,
    connect_args=_connect_args,
)
SessionLocal = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)


@event.listens_for(engine.sync_engine, "connect")
def _sqlite_pragma(dbapi_connection, _connection_record) -> None:  # noqa: ANN001
    if not settings.database_url.startswith("sqlite"):
        return
    cursor = dbapi_connection.cursor()
    try:
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA busy_timeout=30000")
        cursor.execute("PRAGMA synchronous=NORMAL")
    finally:
        cursor.close()


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

    result = await conn.execute(text("PRAGMA table_info(template_exercises)"))
    te_cols = {row[1] for row in result}
    if te_cols and "machine_name" not in te_cols:
        await conn.execute(
            text("ALTER TABLE template_exercises ADD COLUMN machine_name VARCHAR(128)")
        )

    result = await conn.execute(text("PRAGMA table_info(exercise_archive)"))
    arch_cols = {row[1] for row in result}
    if arch_cols and "machine_name" not in arch_cols:
        await conn.execute(
            text("ALTER TABLE exercise_archive ADD COLUMN machine_name VARCHAR(128)")
        )

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
    if "age" not in cols:
        await conn.execute(text("ALTER TABLE users ADD COLUMN age INTEGER"))
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
    if "ai_session_enabled" not in cols:
        await conn.execute(
            text("ALTER TABLE users ADD COLUMN ai_session_enabled BOOLEAN DEFAULT 1")
        )
    if "ai_week_enabled" not in cols:
        await conn.execute(
            text("ALTER TABLE users ADD COLUMN ai_week_enabled BOOLEAN DEFAULT 1")
        )
    else:
        # Column existed with old DEFAULT 0 — bump SQLite default is a no-op; backfill below
        pass
    if "ai_dest" not in cols:
        await conn.execute(
            text("ALTER TABLE users ADD COLUMN ai_dest VARCHAR(16) DEFAULT 'both'")
        )
    # One-shot backfill to new product defaults (flag in app_settings)
    flag = await conn.execute(
        text("SELECT value FROM app_settings WHERE key = 'ai_prefs_default_both_v1'")
    )
    if flag.fetchone() is None:
        await conn.execute(
            text(
                "UPDATE users SET ai_session_enabled = 1, ai_week_enabled = 1, ai_dest = 'both'"
            )
        )
        await conn.execute(
            text(
                "INSERT OR REPLACE INTO app_settings (key, value) "
                "VALUES ('ai_prefs_default_both_v1', '1')"
            )
        )

    result = await conn.execute(text("PRAGMA table_info(group_chat)"))
    gcols = {row[1] for row in result}
    if gcols and "week_digest_hour" not in gcols:
        await conn.execute(
            text("ALTER TABLE group_chat ADD COLUMN week_digest_hour INTEGER DEFAULT 20")
        )
    if gcols and "week_digest_weekday" not in gcols:
        await conn.execute(
            text("ALTER TABLE group_chat ADD COLUMN week_digest_weekday INTEGER DEFAULT 6")
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
    if "planned_kg" not in cols:
        await conn.execute(text("ALTER TABLE session_sets ADD COLUMN planned_kg FLOAT"))
    if "planned_reps" not in cols:
        await conn.execute(text("ALTER TABLE session_sets ADD COLUMN planned_reps INTEGER"))
    if "planned_rpe" not in cols:
        await conn.execute(text("ALTER TABLE session_sets ADD COLUMN planned_rpe INTEGER"))
    if "plan_source" not in cols:
        await conn.execute(
            text("ALTER TABLE session_sets ADD COLUMN plan_source VARCHAR(16)")
        )

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

    result = await conn.execute(text("PRAGMA table_info(coach_usage_logs)"))
    usage_cols = {row[1] for row in result}
    if usage_cols and "provider" not in usage_cols:
        await conn.execute(
            text("ALTER TABLE coach_usage_logs ADD COLUMN provider VARCHAR(32)")
        )
    if usage_cols and "quota_cost" not in usage_cols:
        await conn.execute(
            text("ALTER TABLE coach_usage_logs ADD COLUMN quota_cost INTEGER DEFAULT 0")
        )
    if usage_cols and "request_text" not in usage_cols:
        await conn.execute(
            text("ALTER TABLE coach_usage_logs ADD COLUMN request_text TEXT")
        )
    if usage_cols and "response_text" not in usage_cols:
        await conn.execute(
            text("ALTER TABLE coach_usage_logs ADD COLUMN response_text TEXT")
        )


async def get_session() -> AsyncGenerator[AsyncSession, None]:
    async with SessionLocal() as session:
        yield session
