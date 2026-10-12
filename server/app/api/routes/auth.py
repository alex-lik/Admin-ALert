"""Вход админа и регистрация устройств (задача 2.2)."""

from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, HTTPException, Path, status
from sqlalchemy import select

from app.api.deps import CurrentAdmin, SessionDep
from app.config import get_settings
from app.models.entities import Device
from app.schemas.auth import (
    AdminOut,
    DeliveryReceipt,
    DeviceOut,
    DeviceRegisterRequest,
    LoginRequest,
    RefreshRequest,
    TokenPair,
)
from app.services import auth as auth_service
from app.services.alerts import new_id

router = APIRouter()


@router.post("/auth/login", response_model=TokenPair)
async def login(session: SessionDep, payload: LoginRequest) -> TokenPair:
    """Обменять логин и пароль на пару токенов."""
    try:
        admin = await auth_service.authenticate(session, payload.email, payload.password)
    except auth_service.AuthError as exc:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=str(exc)) from exc

    access, refresh = auth_service.create_tokens(admin)
    return TokenPair(
        access_token=access,
        refresh_token=refresh,
        expires_in=get_settings().access_token_ttl_seconds,
        admin=AdminOut.model_validate(admin),
    )


@router.post("/auth/refresh", response_model=TokenPair)
async def refresh(session: SessionDep, payload: RefreshRequest) -> TokenPair:
    """Обновить access-токен по refresh-токену."""
    try:
        claims = auth_service.decode_token(payload.refresh_token, expected_type="refresh")
    except auth_service.AuthError as exc:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=str(exc)) from exc

    admin = await auth_service.get_admin_by_id(session, str(claims["sub"]))
    if admin is None or not admin.is_active:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="Админ не найден или отключён"
        )

    access, new_refresh = auth_service.create_tokens(admin)
    return TokenPair(
        access_token=access,
        refresh_token=new_refresh,
        expires_in=get_settings().access_token_ttl_seconds,
        admin=AdminOut.model_validate(admin),
    )


@router.get("/auth/me", response_model=AdminOut)
async def me(admin: CurrentAdmin) -> AdminOut:
    """Кто я — клиент спрашивает это при старте, чтобы понять, валиден ли токен."""
    return AdminOut.model_validate(admin)


@router.post("/devices", response_model=DeviceOut, status_code=status.HTTP_201_CREATED)
async def register_device(
    session: SessionDep,
    admin: CurrentAdmin,
    payload: DeviceRegisterRequest,
) -> DeviceOut:
    """Зарегистрировать устройство с push-токеном.

    Повторная регистрация того же токена обновляет запись, а не плодит дубли:
    `onTokenRefresh` прилетает на каждый запуск приложения.
    """
    result = await session.execute(select(Device).where(Device.token == payload.token))
    device = result.scalar_one_or_none()

    if device is None:
        device = Device(
            id=new_id("dev"),
            admin_id=admin.id,
            token=payload.token,
            platform=payload.platform,
            platform_label=payload.platform_label,
            last_seen_at=datetime.now(UTC),
            is_active=True,
        )
        session.add(device)
    else:
        # Токен мог перейти другому админу (например, списали устройство).
        device.admin_id = admin.id
        device.platform = payload.platform
        device.platform_label = payload.platform_label
        device.is_active = True
        device.last_seen_at = datetime.now(UTC)

    await session.flush()
    return DeviceOut.model_validate(device)


@router.get("/devices", response_model=list[DeviceOut])
async def list_devices(session: SessionDep, admin: CurrentAdmin) -> list[DeviceOut]:
    """Устройства текущего админа."""
    result = await session.execute(select(Device).where(Device.admin_id == admin.id))
    return [DeviceOut.model_validate(d) for d in result.scalars()]


@router.post("/devices/heartbeat")
async def heartbeat(session: SessionDep, admin: CurrentAdmin) -> dict[str, str]:
    """Пульс устройства: сервер знает, что телефон на связи и до него доходит.

    Если пульсов нет, эскалация идёт на звонок раньше (задача 2.21).
    """
    now = datetime.now(UTC)
    result = await session.execute(select(Device).where(Device.admin_id == admin.id))
    for device in result.scalars():
        device.last_seen_at = now
    await session.flush()
    return {"status": "ok"}


@router.post("/devices/delivery")
async def delivery_receipt(
    session: SessionDep,
    _admin: CurrentAdmin,
    payload: DeliveryReceipt,
) -> dict[str, str]:
    """Подтверждение доставки push (задачи 2.20 и 3.22).

    Отдельно от heartbeat: «доставлено системой» и «пользователь увидел» —
    разные факты, и для метрик нужны оба.
    """
    result = await session.execute(select(Device).where(Device.token == payload.token))
    device = result.scalar_one_or_none()

    if device is None:
        # Не 404: токен мог отозваться после resolve. Это нормальный путь.
        return {"status": "ignored", "reason": "устройство не найдено"}

    device.last_delivery_at = payload.delivered_at or datetime.now(UTC)
    await session.flush()
    return {"status": "recorded"}


@router.delete("/devices/{device_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_device(
    session: SessionDep,
    admin: CurrentAdmin,
    device_id: Annotated[str, Path()],
) -> None:
    """Отвязать устройство: админ сдал телефон или сменил симку."""
    result = await session.execute(
        select(Device).where(Device.id == device_id, Device.admin_id == admin.id)
    )
    device = result.scalar_one_or_none()
    if device is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Устройство не найдено")

    await session.delete(device)
    await session.flush()
