from functools import lru_cache

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    bot_token: str = Field(alias="BOT_TOKEN")
    admin_telegram_ids: list[int] = Field(default_factory=list, alias="ADMIN_TELEGRAM_IDS")
    timezone: str = Field(default="Europe/Moscow", alias="TIMEZONE")
    database_url: str = Field(
        default="sqlite+aiosqlite:///./gymflex.db",
        alias="DATABASE_URL",
    )
    reminder_hour: int = Field(default=8, alias="REMINDER_HOUR")
    recap_hour: int = Field(default=22, alias="RECAP_HOUR")
    log_level: str = Field(default="INFO", alias="LOG_LEVEL")
    log_file: str = Field(default="logs/gymflex.log", alias="LOG_FILE")
    log_max_bytes: int = Field(default=2_000_000, alias="LOG_MAX_BYTES")
    log_backup_count: int = Field(default=5, alias="LOG_BACKUP_COUNT")
    # Coach master switch (legacy name NN_ENABLED)
    nn_enabled: bool = Field(default=True, alias="NN_ENABLED")
    nn_timeout_sec: float = Field(default=60.0, alias="NN_TIMEOUT_SEC")
    nn_health_timeout_sec: float = Field(default=5.0, alias="NN_HEALTH_TIMEOUT_SEC")
    # Unused when remote LLM is configured; kept for backwards-compatible .env
    nn_url: str = Field(default="http://nn:8000", alias="NN_URL")
    # Default provider if DB has no override: gemini | deepseek | qwen
    coach_provider: str = Field(default="gemini", alias="COACH_PROVIDER")
    gemini_api_key: str = Field(default="", alias="GEMINI_API_KEY")
    gemini_model: str = Field(default="gemini-2.5-flash-lite", alias="GEMINI_MODEL")
    gemini_rpd_limit: int = Field(default=1000, alias="GEMINI_RPD_LIMIT")
    gemini_rpm_limit: int = Field(default=15, alias="GEMINI_RPM_LIMIT")
    deepseek_api_key: str = Field(default="", alias="DEEPSEEK_API_KEY")
    deepseek_base_url: str = Field(
        default="https://api.deepseek.com", alias="DEEPSEEK_BASE_URL"
    )
    deepseek_model: str = Field(default="deepseek-chat", alias="DEEPSEEK_MODEL")
    qwen_api_key: str = Field(default="", alias="QWEN_API_KEY")
    qwen_base_url: str = Field(
        default="https://dashscope-intl.aliyuncs.com/compatible-mode/v1",
        alias="QWEN_BASE_URL",
    )
    qwen_model: str = Field(default="qwen-flash", alias="QWEN_MODEL")
    coach_profile_enabled: bool = Field(default=False, alias="COACH_PROFILE_ENABLED")
    coach_live_cooldown_sec: float = Field(default=90.0, alias="COACH_LIVE_COOLDOWN_SEC")

    @field_validator("admin_telegram_ids", mode="before")
    @classmethod
    def parse_admin_ids(cls, value: object) -> list[int]:
        if value is None or value == "":
            return []
        if isinstance(value, list):
            return [int(v) for v in value]
        if isinstance(value, int):
            return [value]
        text = str(value).strip()
        if not text:
            return []
        return [int(part.strip()) for part in text.split(",") if part.strip()]

    @field_validator("nn_enabled", mode="before")
    @classmethod
    def parse_nn_enabled(cls, value: object) -> bool:
        return _parse_bool(value, default=True)

    @field_validator("coach_profile_enabled", mode="before")
    @classmethod
    def parse_coach_profile(cls, value: object) -> bool:
        return _parse_bool(value, default=False)


def _parse_bool(value: object, *, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    if value is None or value == "":
        return default
    text = str(value).strip().lower()
    if text in {"0", "false", "no", "off", "disabled"}:
        return False
    if text in {"1", "true", "yes", "on", "enabled"}:
        return True
    return bool(value)


@lru_cache
def get_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]
