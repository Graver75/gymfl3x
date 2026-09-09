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


@lru_cache
def get_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]
