"""Схемы расписания дежурства (задача 2.10)."""

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


class ShiftCreate(BaseModel):
    admin_id: str = Field(min_length=1, max_length=64)
    role: str = Field(default="primary", pattern="^(primary|backup)$")
    starts_at: datetime
    ends_at: datetime


class RotationCreate(BaseModel):
    """Развернуть ротацию по кругу.

    Порядок `admin_ids` — это порядок дежурства, а не список «кто-нибудь»:
    первый дежурит с `starts_at`, дальше по кругу.
    """

    admin_ids: list[str] = Field(min_length=1, max_length=20)
    role: str = Field(default="primary", pattern="^(primary|backup)$")
    starts_at: datetime
    shift_hours: int = Field(ge=1, le=168, description="Длительность смены в часах")
    days: int = Field(default=30, ge=1, le=90, description="На сколько дней развернуть")


class ShiftOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    admin_id: str
    role: str
    starts_at: datetime
    ends_at: datetime


class RosterOut(BaseModel):
    """Кто дежурит сейчас по каждой роли."""

    role: str
    shift_id: str | None
    admin_id: str | None
    admin_name: str | None
    starts_at: datetime | None
    ends_at: datetime | None
    # Почему может быть None: расписание не заведено, в нём дыра или
    # дежурный отключён. Клиенту это нужно показать, а не молча уводить
    # на фолбэк.
    reason: str | None = None


class GapOut(BaseModel):
    """Дыра в расписании с явным типом вместо dict.

    Отдельная схема, а не `dict[str, str]`: у дыры всегда есть `from`, `to` и
    `reason`, и опечатка в ключе должна ломаться на сборке ответа, а не на
    экране у дежурного ночью. Поля названы как в JSON ответа сервиса, поэтому
    и называются `from_at` с алиасом.
    """

    model_config = ConfigDict(populate_by_name=True)

    from_at: datetime = Field(alias="from")
    to_at: datetime = Field(alias="to")
    reason: str


class CoverageOut(BaseModel):
    from_at: datetime
    to_at: datetime
    gaps: list[GapOut]
    covered: bool


class RotationOut(BaseModel):
    created: int
    role: str
    from_at: datetime
    to_at: datetime
