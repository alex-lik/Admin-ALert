"""Конфигурация приложения. Все настройки — через переменные окружения (.env)."""

from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    app_name: str = "Admin-ALert"
    environment: str = "dev"
    debug: bool = True
    version: str = Field(default="0.1.0", description="Дублируется из корневого файла VERSION")

    # --- хранилище ---
    database_url: str = "postgresql+asyncpg://adminalert:adminalert@localhost:5432/adminalert"
    redis_url: str = "redis://localhost:6379/0"

    # --- аутентификация ---
    jwt_secret: str = "CHANGE-ME"  # noqa: S105 — placeholder, в проде только из окружения
    jwt_algorithm: str = "HS256"
    access_token_ttl_seconds: int = 3600
    refresh_token_ttl_seconds: int = 60 * 60 * 24 * 30

    # --- воркер эскалации (задача 1.4) ---
    # Таймеры живут в БД (next_action_at), воркер только опрашивает — так они переживают рестарт.
    worker_poll_interval_seconds: int = 5
    worker_batch_size: int = 100

    # --- приём событий ---
    events_rate_limit_per_minute: int = 600

    @property
    def is_production(self) -> bool:
        return self.environment == "prod"


@lru_cache
def get_settings() -> Settings:
    return Settings()
