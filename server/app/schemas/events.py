"""Единый формат входящего события (план, раздел 1.6).

Любой источник — Alertmanager, Uptime Kuma, Zabbix, curl из cron, desktop-агент —
приводится адаптером к этой схеме.
"""

from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, Field, field_validator


class Severity(StrEnum):
    WARNING = "warning"
    CRITICAL = "critical"


class EventStatus(StrEnum):
    FIRING = "firing"
    RESOLVED = "resolved"


class IncomingEvent(BaseModel):
    """Событие от источника алертов."""

    project: str = Field(description="Идентификатор проекта в системе", examples=["flytaxi"])
    host: str = Field(description="Хост или источник проверки", examples=["db-master-01"])
    check: str = Field(description="Имя проверки", examples=["disk_space", "api_latency"])
    severity: Severity = Field(description="Уровень: warning игнорирует тихие часы, critical — нет")
    summary: str = Field(description="Что случилось, человеческим языком", max_length=500)
    status: EventStatus = Field(default=EventStatus.FIRING)
    runbook_url: str | None = Field(default=None, description="Ссылка на инструкцию")
    dedup_key: str | None = Field(
        default=None,
        description="Ключ дедупликации. Если не задан — соберётся из project:check:host.",
    )
    occurred_at: datetime | None = Field(
        default=None, description="Время события на стороне источника"
    )
    labels: dict[str, str] = Field(default_factory=dict, description="Произвольные метки источника")

    @field_validator("summary")
    @classmethod
    def summary_not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("summary не может быть пустым")
        return value.strip()

    @property
    def resolved_dedup_key(self) -> str:
        """Ключ дедупликации: явный из источника либо собранный."""
        return self.dedup_key or f"{self.project}:{self.check}:{self.host}"


class EventAccepted(BaseModel):
    """Ответ на POST /events — 202 Accepted, обработка асинхронная."""

    alert_id: str
    dedup_key: str
    status: EventStatus
    occurrences: int = Field(description="Счётчик повторов по dedup_key")
    duplicate: bool = Field(description="True — событие схлопнулось с уже идущим алертом")
