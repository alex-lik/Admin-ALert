"""Схемы API для алертов (задачи 2.14 и 3.2).

Формы повторяют то, что уже читает клиент: `client/src/types/alert.ts`.
Расхождение между ними — источник тихих багов, поэтому при изменении
меняются оба файла.
"""

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from app.models.enums import AlertState, Severity


class ProjectOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    name: str
    is_vip: bool


class HostOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    name: str


class CheckOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    key: str
    description: str | None = None


class AlertStepOut(BaseModel):
    """Шаг истории эскалации. Клиент рисует это таймлайном."""

    model_config = ConfigDict(from_attributes=True)

    id: str
    step: int
    channel: str
    target: str | None = None
    at: datetime
    outcome: str
    note: str | None = None


class AlertBrief(BaseModel):
    """Карточка в списке. Ровно то, что нужно для отрисовки."""

    model_config = ConfigDict(from_attributes=True)

    id: str
    project: ProjectOut
    host: HostOut
    check: CheckOut
    severity: Severity
    state: AlertState
    summary: str
    dedup_key: str
    # Имя профиля эскалации. В БД поле называется profile_id, в API — profile:
    # «id» здесь вводит в заблуждение, профили именуемые (warning, critical).
    profile: str
    occurrences: int
    first_seen_at: datetime
    last_seen_at: datetime
    next_action_at: datetime | None = None
    acked_at: datetime | None = None
    acked_by: str | None = None


class AlertDetail(AlertBrief):
    """Полная карточка алерта."""

    detail: str | None = None
    runbook_url: str | None = None
    snoozed_until: datetime | None = None
    escalation_history: list[AlertStepOut] = Field(default_factory=list)


class AlertListResponse(BaseModel):
    items: list[AlertBrief]
    total: int
    # Сколько активных всего, независимо от фильтра — для бейджей наверху.
    active_count: int
    critical_count: int


class AckRequest(BaseModel):
    comment: str | None = Field(default=None, max_length=500)


class SnoozeRequest(BaseModel):
    minutes: int = Field(ge=1, le=120, description="Жёсткий лимит: отложить всё подряд нельзя")
    comment: str | None = Field(default=None, max_length=500)


class ReassignRequest(BaseModel):
    admin_id: str
    comment: str | None = Field(default=None, max_length=500)


class AckResponse(BaseModel):
    alert_id: str
    state: AlertState
    acked_at: datetime
    acked_by: str
    # Что именно остановилось — показываем админу, чтобы он понимал последствия.
    stopped: list[str]
