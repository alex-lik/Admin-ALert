"""Схемы аутентификации и устройств (задача 2.2)."""

from datetime import datetime

from pydantic import BaseModel, ConfigDict, EmailStr, Field


class LoginRequest(BaseModel):
    email: EmailStr
    password: str = Field(min_length=8, max_length=128)


class TokenPair(BaseModel):
    access_token: str
    refresh_token: str
    token_type: str = "bearer"  # noqa: S105 — это OAuth-тип, не секрет
    expires_in: int
    admin: "AdminOut"


class RefreshRequest(BaseModel):
    refresh_token: str


class AdminOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    name: str
    email: str
    role: str
    timezone: str


class DeviceRegisterRequest(BaseModel):
    token: str = Field(min_length=8, description="FCM или APNs токен")
    platform: str = Field(pattern="^(android|ios)$")
    platform_label: str | None = Field(default=None, max_length=120)


class DeviceOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    platform: str
    platform_label: str | None = None
    last_seen_at: datetime | None = None
    is_active: bool


class DeliveryReceipt(BaseModel):
    """Клиент сообщает, что push доставлен (задачи 2.20 и 3.22)."""

    token: str
    delivered_at: datetime | None = None


TokenPair.model_rebuild()
