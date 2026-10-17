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
    # Устройство считается молчащим, если heartbeat старше этого. Молчащий
    # телефон push не ждёт: шаг помечается сорванным, эскалация ускоряется
    # в сторону звонка (задача 2.21).
    heartbeat_stale_seconds: int = 900

    # --- приём событий ---
    events_rate_limit_per_minute: int = 600

    # --- push (задачи 1.7, 2.16–2.18) ---
    # Файл ключа сервисного аккаунта Firebase. Пусто — FCM не настроен, и воркер
    # честно пишет в аудите `skipped`, а не выдуманное `delivered`.
    fcm_service_account_file: str | None = None
    # Переопределяет project_id из файла, если ключ от другого проекта.
    fcm_project_id: str | None = None
    # Critical Alerts на iOS требуют entitlement от Apple. Без него FCM вернёт
    # BadPriority, и уведомление не дойдёт вовсе — поэтому по умолчанию выключено.
    apns_critical_alerts: bool = False

    @property
    def is_production(self) -> bool:
        return self.environment == "prod"


@lru_cache
def get_settings() -> Settings:
    return Settings()
