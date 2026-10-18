"""Тесты HTTP-слоя расписания дежурства (задача 2.10).

Проверяется то, что не проверяется в сервисах: контракт для клиента. В
первую очередь — что «кто дежурит сейчас» отвечает полезно даже при пустом
расписании, потому что это первое, что открывает дежурный ночью.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import timedelta
from typing import Any

import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.db import get_session
from app.main import create_app
from app.models import Base
from app.services.alerts import now
from app.services.auth import register_admin

ADMIN_EMAIL = "admin@utax.top"
ADMIN_PASSWORD = "verysecret123"


@pytest_asyncio.fixture
async def session() -> AsyncIterator[AsyncSession]:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    factory = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    async with factory() as db:
        yield db

    await engine.dispose()


@pytest_asyncio.fixture
async def crew(session: AsyncSession) -> list:
    """Три админа. Первый — с email из ADMIN_EMAIL, чтобы вход в тестах был
    одинаковым: расписание меняет любой дежурный, отдельной учётки для него
    не существует."""
    emails = [ADMIN_EMAIL, "d1@utax.top", "d2@utax.top"]
    return [
        await register_admin(session, email, f"Дежурный {i}", ADMIN_PASSWORD)
        for i, email in enumerate(emails)
    ]


@pytest_asyncio.fixture
async def client(session: AsyncSession) -> AsyncIterator[AsyncClient]:
    app = create_app()

    async def _override() -> AsyncIterator[AsyncSession]:
        yield session

    app.dependency_overrides[get_session] = _override

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as http:
        yield http


async def login(client: AsyncClient, email: str = ADMIN_EMAIL) -> dict[str, str]:
    response = await client.post(
        "/api/v1/auth/login", json={"email": email, "password": ADMIN_PASSWORD}
    )
    assert response.status_code == 200, response.text
    return {"Authorization": f"Bearer {response.json()['access_token']}"}


def iso(dt: Any) -> str:
    return dt.isoformat()


# ───────────────────────────── Текущий состав ─────────────────────────────


async def test_roster_without_schedule_explains_why(client: AsyncClient, crew: list) -> None:
    """Пустое расписание — не «дежурный неизвестен», а объяснение, что его
    никто не завёл. Иначе дежурный ночью не поймёт, куда смотреть."""
    headers = await login(client)

    response = await client.get("/api/v1/oncall/roster", headers=headers)

    assert response.status_code == 200
    body = response.json()
    assert {r["role"] for r in body} == {"primary", "backup"}
    assert all(r["admin_id"] is None for r in body)
    assert all(r["reason"] == "расписание не заведено" for r in body)


async def test_roster_shows_current(client: AsyncClient, crew: list) -> None:
    # Смена начинается «сейчас», а не час назад: HTTP-слой честно отказывает
    # в смене, начавшейся в прошлом (см. test_past_shift_returns_409).
    start = now()
    headers = await login(client)

    created = await client.post(
        "/api/v1/oncall/shifts",
        json={
            "admin_id": crew[1].id,
            "role": "primary",
            "starts_at": iso(start),
            "ends_at": iso(start + timedelta(hours=8)),
        },
        headers=headers,
    )

    assert created.status_code == 201, created.text

    body = (await client.get("/api/v1/oncall/roster", headers=headers)).json()
    primary = next(r for r in body if r["role"] == "primary")

    assert primary["admin_id"] == crew[1].id
    assert primary["admin_name"] == crew[1].name
    assert primary["reason"] is None


async def test_whoami_reports_on_call(client: AsyncClient, crew: list) -> None:
    """Клиенту нужно знать «вы сейчас на связи» или «вы запасной»."""
    start = now()
    headers = await login(client, crew[0].email)

    await client.post(
        "/api/v1/oncall/shifts",
        json={
            "admin_id": crew[0].id,
            "role": "primary",
            "starts_at": iso(start),
            "ends_at": iso(start + timedelta(hours=8)),
        },
        headers=headers,
    )

    body = (await client.get("/api/v1/oncall/whoami", headers=headers)).json()

    assert body["on_call"] is True
    assert body["roles"] == ["primary"]
    assert body["current_shift"]["role"] == "primary"


async def test_whoami_when_not_on_call(client: AsyncClient, crew: list) -> None:
    headers = await login(client)

    body = (await client.get("/api/v1/oncall/whoami", headers=headers)).json()

    assert body["on_call"] is False
    assert body["roles"] == []
    assert body["current_shift"] is None


async def test_whoami_names_the_backup(client: AsyncClient, crew: list) -> None:
    """Подсказка «кто запасной» отвечает на вопрос «кого позовут, если я
    не отвечу»."""
    start = now()
    headers = await login(client, crew[0].email)

    for role, admin in (("primary", crew[0]), ("backup", crew[1])):
        await client.post(
            "/api/v1/oncall/shifts",
            json={
                "admin_id": admin.id,
                "role": role,
                "starts_at": iso(start),
                "ends_at": iso(start + timedelta(hours=8)),
            },
            headers=headers,
        )

    body = (await client.get("/api/v1/oncall/whoami", headers=headers)).json()

    assert body["fallback_would_call"] == crew[1].name


# ───────────────────────────── Запись расписания ─────────────────────────────


async def test_rotation_creates_shifts(client: AsyncClient, crew: list) -> None:
    headers = await login(client)

    response = await client.post(
        "/api/v1/oncall/rotation",
        json={
            "admin_ids": [a.id for a in crew],
            "role": "primary",
            "starts_at": iso(now() + timedelta(hours=1)),
            "shift_hours": 12,
            "days": 2,
        },
        headers=headers,
    )

    assert response.status_code == 201, response.text
    assert response.json()["created"] == 4, "2 дня по 12 часов = 4 смены"


async def test_overlapping_shift_returns_409(client: AsyncClient, crew: list) -> None:
    """409, а не 500: конфликт расписания это ожидаемый ответ на ожидаемую
    ошибку администратора."""
    headers = await login(client)
    start = now() + timedelta(hours=1)
    payload = {
        "admin_id": crew[0].id,
        "role": "primary",
        "starts_at": iso(start),
        "ends_at": iso(start + timedelta(hours=12)),
    }

    first = await client.post("/api/v1/oncall/shifts", json=payload, headers=headers)
    second = await client.post(
        "/api/v1/oncall/shifts",
        json={**payload, "admin_id": crew[1].id, "starts_at": iso(start + timedelta(hours=6))},
        headers=headers,
    )

    assert first.status_code == 201
    assert second.status_code == 409
    assert "пересекается" in second.json()["detail"]


async def test_rotation_over_schedule_returns_409(client: AsyncClient, crew: list) -> None:
    """Ротация поверх ручной смены обязана откатиться целиком.

    Частичное создание оставило бы расписание в состоянии, которое никто не
    задумывал: половина круга новая, половина старая.
    """
    headers = await login(client)
    start = now() + timedelta(hours=1)
    await client.post(
        "/api/v1/oncall/shifts",
        json={
            "admin_id": crew[2].id,
            "role": "primary",
            "starts_at": iso(start),
            "ends_at": iso(start + timedelta(hours=24)),
        },
        headers=headers,
    )

    response = await client.post(
        "/api/v1/oncall/rotation",
        json={
            "admin_ids": [a.id for a in crew],
            "role": "primary",
            "starts_at": iso(start),
            "shift_hours": 6,
            "days": 1,
        },
        headers=headers,
    )

    assert response.status_code == 409

    shifts = (await client.get("/api/v1/oncall/shifts", headers=headers)).json()
    assert len(shifts) == 1, "частичная ротация не должна оставаться в БД"


async def test_past_shift_returns_409(client: AsyncClient, crew: list) -> None:
    headers = await login(client)

    response = await client.post(
        "/api/v1/oncall/shifts",
        json={
            "admin_id": crew[0].id,
            "role": "primary",
            "starts_at": iso(now() - timedelta(days=1)),
            "ends_at": iso(now() + timedelta(days=1)),
        },
        headers=headers,
    )

    assert response.status_code == 409
    assert "прошлом" in response.json()["detail"]


async def test_invalid_role_rejected_by_schema(client: AsyncClient, crew: list) -> None:
    """Опечатка в роли отсекается схемой, а не падает на сервисе."""
    headers = await login(client)

    response = await client.post(
        "/api/v1/oncall/shifts",
        json={
            "admin_id": crew[0].id,
            "role": "начальник",
            "starts_at": iso(now() + timedelta(hours=1)),
            "ends_at": iso(now() + timedelta(hours=2)),
        },
        headers=headers,
    )

    assert response.status_code == 422


async def test_delete_shift(client: AsyncClient, crew: list) -> None:
    headers = await login(client)
    start = now() + timedelta(hours=1)
    created = await client.post(
        "/api/v1/oncall/shifts",
        json={
            "admin_id": crew[0].id,
            "role": "primary",
            "starts_at": iso(start),
            "ends_at": iso(start + timedelta(hours=4)),
        },
        headers=headers,
    )

    response = await client.delete(f"/api/v1/oncall/shifts/{created.json()['id']}", headers=headers)

    assert response.status_code == 204
    assert (await client.get("/api/v1/oncall/shifts", headers=headers)).json() == []


async def test_delete_unknown_shift_returns_404(client: AsyncClient, crew: list) -> None:
    headers = await login(client)

    response = await client.delete("/api/v1/oncall/shifts/shf_нет", headers=headers)

    assert response.status_code == 404


# ───────────────────────────── Покрытие ─────────────────────────────


async def test_coverage_reports_gap(client: AsyncClient, crew: list) -> None:
    """Молчаливая дырка опаснее явной ошибки: система работает, но дежурит не
    тот. Покрытие обязано её показывать."""
    headers = await login(client)

    response = await client.get("/api/v1/oncall/coverage", headers=headers)

    assert response.status_code == 200
    body = response.json()
    assert body["covered"] is False
    assert body["gaps"], "пустое расписание — это сплошная дыра"


async def test_coverage_full_after_rotation(client: AsyncClient, crew: list) -> None:
    headers = await login(client)
    await client.post(
        "/api/v1/oncall/rotation",
        json={
            "admin_ids": [a.id for a in crew],
            "role": "primary",
            "starts_at": iso(now()),
            "shift_hours": 6,
            "days": 3,
        },
        headers=headers,
    )

    body = (await client.get("/api/v1/oncall/coverage?hours=12", headers=headers)).json()

    assert body["covered"] is True
    assert body["gaps"] == []


async def test_horizon_endpoint(client: AsyncClient, crew: list) -> None:
    """Ротация материализована, её нужно продолжать — эндпоинт позволяет
    проверить это вручную."""
    headers = await login(client)
    await client.post(
        "/api/v1/oncall/rotation",
        json={
            "admin_ids": [a.id for a in crew],
            "role": "primary",
            "starts_at": iso(now()),
            "shift_hours": 12,
            "days": 2,
        },
        headers=headers,
    )

    response = await client.post("/api/v1/oncall/horizon", headers=headers)

    assert response.status_code == 200
    assert response.json()["created"] > 0


# ───────────────────────────── Доступ ─────────────────────────────


async def test_roster_requires_auth(client: AsyncClient, crew: list) -> None:
    assert (await client.get("/api/v1/oncall/roster")).status_code == 401


async def test_shift_write_requires_auth(client: AsyncClient, crew: list) -> None:
    """Запись в расписание без токена невозможна: иначе расписание менял бы
    кто угодно."""
    response = await client.post(
        "/api/v1/oncall/shifts",
        json={
            "admin_id": crew[0].id,
            "role": "primary",
            "starts_at": iso(now() + timedelta(hours=1)),
            "ends_at": iso(now() + timedelta(hours=2)),
        },
    )

    assert response.status_code == 401
