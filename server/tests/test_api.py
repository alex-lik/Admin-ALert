"""Тесты health и валидации приёма событий.

Сценарии приёма (дедупликация, ack, устройства) живут в `test_api_alerts.py`.

Здесь важно другое: сессия БД обязана быть асинхронной. Модели описаны на
async SQLAlchemy, и синхронная сессия падает невнятно:
«ChunkedIteratorResult can't be used in 'await' expression».
"""

from collections.abc import AsyncIterator

import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.db import get_session
from app.main import create_app
from app.models import Base


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
async def client(session: AsyncSession) -> AsyncIterator[AsyncClient]:
    app = create_app()

    async def _override() -> AsyncIterator[AsyncSession]:
        yield session

    app.dependency_overrides[get_session] = _override

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as http:
        yield http


def valid_payload(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "project": "flytaxi",
        "host": "db-master-01",
        "check": "disk_space",
        "severity": "critical",
        "summary": "Диск заполнен на 95%",
    }
    payload.update(overrides)
    return payload


async def test_health(client: AsyncClient) -> None:
    response = await client.get("/api/v1/health")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"


async def test_event_requires_api_key(client: AsyncClient) -> None:
    response = await client.post("/api/v1/events", json=valid_payload())
    assert response.status_code == 401


async def test_event_rejects_blank_summary(client: AsyncClient) -> None:
    """Пустая сводка не должна доходить до БД."""
    response = await client.post(
        "/api/v1/events", json=valid_payload(summary="   "), headers={"X-API-Key": "k"}
    )
    assert response.status_code == 422


async def test_event_rejects_unknown_severity(client: AsyncClient) -> None:
    """Неизвестный уровень отклоняется, а не молча становится warning:
    молчаливое понижение критичного алерта опаснее его отсутствия."""
    response = await client.post(
        "/api/v1/events", json=valid_payload(severity="catastrophe"), headers={"X-API-Key": "k"}
    )
    assert response.status_code == 422


async def test_event_rejects_unknown_api_key(client: AsyncClient, session: AsyncSession) -> None:
    from app.models import Project

    session.add(Project(id="p", name="p", is_vip=False, api_key="correct-key"))
    await session.flush()

    # Ключ в ASCII: значение заголовка HTTP по спецификации не может
    # содержать не-ASCII, кириллица в нём просто невозможна.
    response = await client.post(
        "/api/v1/events", json=valid_payload(), headers={"X-API-Key": "wrong-key"}
    )
    assert response.status_code == 401


async def test_malformed_body_is_reported_before_key_check(
    client: AsyncClient,
    session: AsyncSession,
) -> None:
    """Ключ проверяется после формы, а не до неё.

    Иначе источник, отправивший пустую сводку, получил бы 401 и решил бы,
    что у него неверный ключ, вместо того чтобы чинить свой запрос.
    """
    from app.models import Project

    session.add(Project(id="p", name="p", is_vip=False, api_key="valid-key"))
    await session.flush()

    response = await client.post(
        "/api/v1/events", json=valid_payload(summary=""), headers={"X-API-Key": "valid-key"}
    )
    assert response.status_code == 422
    assert response.json()["detail"][0]["loc"][-1] == "summary"


def test_dedup_key_is_built_from_project_check_host() -> None:
    """Схема собирает ключ, даже если источник его не прислал."""
    from app.schemas.events import IncomingEvent

    event = IncomingEvent(**valid_payload())  # type: ignore[arg-type]
    assert event.resolved_dedup_key == "flytaxi:disk_space:db-master-01"


def test_explicit_dedup_key_wins_over_derived() -> None:
    from app.schemas.events import IncomingEvent

    event = IncomingEvent(**valid_payload(dedup_key="мой-ключ"))  # type: ignore[arg-type]
    assert event.resolved_dedup_key == "мой-ключ"
