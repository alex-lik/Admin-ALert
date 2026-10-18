"""Тесты health-check и готовности (задача 2.23).

Главный тест здесь — про `backlog`. Обычный `/health` показывает, что процесс
жив, и этого недостаточно: при зависшем воркере процесс здоров, а пейджинг не
идёт, и сообщить об этом некому. Готовность обязана учитывать очередь.

Второй по важности — тот, что ловил реальный баг: у инъектированного в
FastAPI объекта `Response` поле `status_code` равно `None`, поэтому сравнение
`response.status_code == 200` всегда давало «not_ready».
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.db import get_session
from app.main import create_app
from app.models import Base
from app.services import metrics as metrics_module
from app.services.metrics import Metrics


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

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as http:
        yield http


def stub(monkeypatch: pytest.MonkeyPatch, due: int) -> None:
    """Подменить очередь воркера.

    Подменяются три вещи, и все три обходят боевую БД:

    - `count_due` — чтобы вернуть нужное число;
    - `SessionLocal` в модуле health — он берёт сессию напрямую, минуя
      переопределение зависимости FastAPI;
    - `engine` — readiness делает `SELECT 1` через настоящий движок, и без
      подмены тест ушёл бы в Postgres из DATABASE_URL.
    """
    from app.api.routes import health
    from app.services import escalation

    async def fake_count_due(_session: Any) -> int:
        return due

    monkeypatch.setattr(escalation, "count_due", fake_count_due)
    monkeypatch.setattr(health, "SessionLocal", fake_session)
    monkeypatch.setattr(health, "engine", _FakeEngine())


class _FakeConn:
    """Соединение, которое на `execute` отвечает успехом."""

    async def __aenter__(self) -> _FakeConn:
        return self

    async def __aexit__(self, *_exc: object) -> None:
        return None

    async def execute(self, *_args: Any, **_kwargs: Any) -> None:
        return None


class _FakeEngine:
    """Движок, который на любой запрос отвечает успехом.

    Нужен, чтобы `SELECT 1` в readiness не ушёл в боевую БД из DATABASE_URL.
    """

    def connect(self) -> _FakeConn:
        return _FakeConn()


@asynccontextmanager
async def fake_session() -> AsyncIterator[Any]:
    yield object()


@pytest.fixture(autouse=True)
def healthy_database(monkeypatch: pytest.MonkeyPatch) -> None:
    """По умолчанию БД доступна и очередь пуста: тестам без `stub` нужен
    заведомо исправный ответ, а не поход в боевую базу."""
    from app.api.routes import health
    from app.services import escalation

    async def fake_count_due(_session: Any) -> int:
        return 0

    monkeypatch.setattr(escalation, "count_due", fake_count_due)
    monkeypatch.setattr(health, "SessionLocal", fake_session)
    monkeypatch.setattr(health, "engine", _FakeEngine())


# ───────────────────────────── Базовый health ─────────────────────────────


async def test_health_is_cheap_and_green(client: AsyncClient) -> None:
    """`/health` не трогает БД: его опрашивает балансировщик постоянно."""
    response = await client.get("/api/v1/health")

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["version"]


async def test_health_has_no_backlog(client: AsyncClient) -> None:
    """Очередь воркера в `/health` не нужна: она в `/health/ready`. Иначе
    балансировщик выкидывал бы процесс из ротации из-за зависшего воркера,
    хотя API жив и отвечает."""
    body = (await client.get("/api/v1/health")).json()

    assert "backlog" not in body


# ───────────────────────────── Готовность ─────────────────────────────


async def test_ready_when_everything_fine(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Регрессия на баг с `response.status_code is None`.

    Раньше сравнение с 200 всегда давало False, и исправный сервис отвечал
    `not_ready`. Балансировщик выкинул бы его из ротации, а watchdog слал бы
    тревогу на пустом месте.
    """
    stub(monkeypatch, due=0)

    response = await client.get("/api/v1/health/ready")

    assert response.status_code == 200
    assert response.json()["status"] == "ready"


async def test_ready_reports_backlog(client: AsyncClient, monkeypatch: pytest.MonkeyPatch) -> None:
    stub(monkeypatch, due=3)

    response = await client.get("/api/v1/health/ready")

    assert response.json()["backlog"] == 3


async def test_not_ready_on_big_backlog(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Процесс жив, но эскалация не идёт — это авария, и она должна быть
    видна снаружи."""
    stub(monkeypatch, due=10_000)

    response = await client.get("/api/v1/health/ready")

    assert response.status_code == 503
    body = response.json()
    assert body["status"] == "not_ready"
    assert body["backlog"] == 10_000


async def test_small_backlog_is_fine(client: AsyncClient, monkeypatch: pytest.MonkeyPatch) -> None:
    """Небольшая очередь — нормальная работа: воркер ходит раз в пять секунд,
    между проходами что-то да накапливается."""
    stub(monkeypatch, due=3)

    assert (await client.get("/api/v1/health/ready")).status_code == 200


async def test_backlog_failure_is_not_ready(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Очередь не посчиталась — состояние неизвестно, и объявлять «готов»
    означало бы врать."""
    from app.services import escalation

    async def boom(_session: Any) -> int:
        raise RuntimeError("таймаут")

    monkeypatch.setattr(escalation, "count_due", boom)

    response = await client.get("/api/v1/health/ready")

    assert response.status_code == 503
    assert response.json()["backlog"] == -1


# ───────────────────────────── Метрики ─────────────────────────────


async def test_metrics_endpoint_renders(client: AsyncClient) -> None:
    metrics_module.metrics.increment("test_render_total")

    response = await client.get("/api/v1/metrics")

    assert response.status_code == 200
    assert "text/plain" in response.headers["content-type"]
    assert "# TYPE" in response.text
    assert "test_render_total" in response.text


async def test_metrics_are_prometheus_parseable(client: AsyncClient) -> None:
    """Формат должен принимать прометеус-парсер, иначе сборщик молча
    отбросит всё. Проверяем базовую структуру каждой строки-семпла."""
    metrics_module.metrics.increment("test_parse_total")

    text = (await client.get("/api/v1/metrics")).text
    samples = [line for line in text.splitlines() if line and not line.startswith("#")]

    assert samples, "метрики должны содержать хотя бы одну строку-семпл"
    for line in samples:
        name, _, value = line.rpartition(" ")
        assert name, f"строка без имени: {line}"
        float(value), f"строка без числа: {line}"


def test_render_is_valid_without_any_data() -> None:
    """Пустой вывод тоже должен быть валидным: сборщик опрашивает и до того,
    как что-то случилось."""
    out = Metrics().render()

    assert isinstance(out, str)
    for line in out.splitlines():
        assert line == "" or line.startswith("#") or " " in line


def test_metrics_survive_across_requests() -> None:
    """Счётчики живут в модуле, а не в запросе: иначе метрики обнулялись бы
    на каждом scrape и показывали ноль всегда."""
    metrics_module.metrics.increment("test_counter_total")

    assert "test_counter_total 1" in metrics_module.metrics.render()
