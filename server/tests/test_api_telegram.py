"""Тесты HTTP-слоя Telegram: webhook, привязка чата, статус (задача 4.8).

Здесь проверяется то, чего не видно в тестах транспорта: что webhook
принимает нажатие, применяет действие к алерту и **не даёт подделать** его.

Ключевое отличие от тестов сервиса — `make_dispatcher` внутри роутера создаёт
клиент с настоящим токеном из настроек. Поэтому здесь подменяется сама функция
сборки: так проверяется маршрут целиком, а Bot API при этом не трогается.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.api.routes import telegram as telegram_route
from app.db import get_session
from app.main import create_app
from app.models import (
    Alert,
    AlertState,
    Base,
    EscalationProfile,
    EscalationStepDefinition,
    Host,
    Project,
)
from app.schemas.events import IncomingEvent
from app.services.alerts import ingest_event
from app.services.auth import register_admin
from app.services.dispatch import PushDispatcher
from app.services.telegram import TelegramClient

ADMIN_EMAIL = "admin@utax.top"
ADMIN_PASSWORD = "verysecret123"
# Секрет в заголовке HTTP: значение обязано быть ASCII (latin-1), поэтому
# кириллица здесь была бы невозможна даже при живом Telegram.
WEBHOOK_SECRET = "webhook-secret-9f3a"


class _FakeTelegramTransport(httpx.AsyncBaseTransport):
    """Bot API заглушка: отвечает успехом и запоминает вызовы."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        method = request.url.path.rsplit("/", 1)[-1]
        self.calls.append(
            {"method": method, "payload": json.loads(request.content.decode("utf-8"))}
        )
        if method == "getChat":
            return httpx.Response(200, json={"ok": True, "result": {"id": 555, "type": "private"}})
        return httpx.Response(200, json={"ok": True, "result": {"message_id": 42}})

    def of_method(self, method: str) -> list[dict[str, Any]]:
        return [c for c in self.calls if c["method"] == method]


@pytest.fixture
def bot_transport() -> _FakeTelegramTransport:
    return _FakeTelegramTransport()


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
async def project(session: AsyncSession) -> Project:
    session.add(
        EscalationProfile(id="critical", description="c", respects_quiet_hours=False, max_steps=6)
    )
    session.add(
        EscalationStepDefinition(
            id="esd_1",
            profile_id="critical",
            step=1,
            channel="push",
            delay_seconds=0,
            target="current",
        )
    )
    session.add(
        Project(id="prj_t", name="flytaxi", is_vip=False, api_key="k", default_profile="critical")
    )
    session.add(Host(id="hst_t", project_id="prj_t", name="db-master-01"))
    await session.flush()
    return await session.get(Project, "prj_t")


@pytest_asyncio.fixture
async def admin(session: AsyncSession):
    return await register_admin(session, ADMIN_EMAIL, "Иван Петров", ADMIN_PASSWORD)


@pytest_asyncio.fixture
async def alert(session: AsyncSession, project: Project) -> Alert:
    created, _ = await ingest_event(
        session,
        project,
        IncomingEvent(
            project="flytaxi",
            host="db-master-01",
            check="disk_space",
            severity="critical",
            summary="Диск заполнен",
        ),
    )
    await session.commit()
    return created


@pytest_asyncio.fixture
async def app_and_client(
    session: AsyncSession,
    bot_transport: _FakeTelegramTransport,
    monkeypatch: pytest.MonkeyPatch,
) -> AsyncIterator[tuple[Any, AsyncClient]]:
    """Приложение с подменённым Telegram и общей сессией БД."""
    telegram = TelegramClient("123:ABC", http=httpx.AsyncClient(transport=bot_transport))

    def fake_dispatcher(_settings: object) -> PushDispatcher:
        class _Scope:
            def __init__(self, inner: AsyncSession) -> None:
                self._inner = inner

            async def __aenter__(self) -> AsyncSession:
                return self._inner

            async def __aexit__(self, *_exc: object) -> None:
                return None

        return PushDispatcher(lambda: _Scope(session), None, telegram=telegram)

    monkeypatch.setattr(telegram_route, "make_dispatcher", fake_dispatcher)

    app = create_app()
    app.state.settings = type(
        "Settings",
        (),
        {"telegram_bot_token": "123:ABC", "telegram_webhook_secret": WEBHOOK_SECRET},
    )()

    async def _override() -> AsyncIterator[AsyncSession]:
        yield session

    app.dependency_overrides[get_session] = _override

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as http:
        yield app, http


async def login(client: AsyncClient) -> dict[str, str]:
    response = await client.post(
        "/api/v1/auth/login",
        json={"email": ADMIN_EMAIL, "password": ADMIN_PASSWORD},
    )
    assert response.status_code == 200, response.text
    return {"Authorization": f"Bearer {response.json()['access_token']}"}


def callback_update(data: str, *, chat_id: int = 555, callback_id: str = "cbq_1") -> dict[str, Any]:
    return {
        "update_id": 1,
        "callback_query": {
            "id": callback_id,
            "from": {"id": 777, "is_bot": False, "first_name": "Иван"},
            "chat_instance": "ci",
            "data": data,
            "message": {
                "message_id": 42,
                "chat": {"id": chat_id, "type": "private"},
                "text": "🚨 Диск заполнен",
            },
        },
    }


# ───────────────────────────── Секрет webhook ─────────────────────────────


async def test_webhook_rejects_wrong_secret(app_and_client, alert: Alert) -> None:
    """Без проверки секрета нажатие «Принял» можно подделать любому."""
    _, client = app_and_client

    response = await client.post(
        "/api/v1/telegram/webhook",
        json=callback_update(f"ack:{alert.id}"),
        headers={"X-Telegram-Bot-Api-Secret-Token": "wrong-secret"},
    )

    assert response.status_code == 403


async def test_webhook_without_secret_header_is_rejected(app_and_client, alert: Alert) -> None:
    """Отсутствие заголовка — тоже неверный секрет, а не «проверка не нужна»."""
    _, client = app_and_client

    response = await client.post(
        "/api/v1/telegram/webhook", json=callback_update(f"ack:{alert.id}")
    )

    assert response.status_code == 403


async def test_webhook_accepts_correct_secret(app_and_client, alert: Alert) -> None:
    _, client = app_and_client

    response = await client.post(
        "/api/v1/telegram/webhook",
        json=callback_update(f"ack:{alert.id}"),
        headers={"X-Telegram-Bot-Api-Secret-Token": WEBHOOK_SECRET},
    )

    assert response.status_code == 200
    assert response.json()["handled"] is True


# ───────────────────────────── Ack ─────────────────────────────


async def test_ack_button_confirms_alert(
    app_and_client,
    session: AsyncSession,
    bot_transport: _FakeTelegramTransport,
    alert: Alert,
    admin,
) -> None:
    """Главный сценарий: нажали «Принял» — эскалация остановлена."""
    _, client = app_and_client
    admin.telegram_chat_id = "555"
    await session.commit()

    response = await client.post(
        "/api/v1/telegram/webhook",
        json=callback_update(f"ack:{alert.id}"),
        headers={"X-Telegram-Bot-Api-Secret-Token": WEBHOOK_SECRET},
    )

    assert response.status_code == 200
    assert response.json()["result"] == "acked"

    await session.refresh(alert)
    assert alert.state == AlertState.ACKED
    assert alert.acked_by_id == admin.id
    assert alert.next_action_at is None, "таймер должен быть остановлен"


async def test_ack_records_telegram_in_audit(
    app_and_client, session: AsyncSession, bot_transport, alert: Alert, admin
) -> None:
    """В аудите должно быть видно, что подтвердили из Telegram, а не из приложения."""
    from sqlalchemy import select

    from app.models import AlertStep

    _, client = app_and_client
    admin.telegram_chat_id = "555"
    await session.commit()

    await client.post(
        "/api/v1/telegram/webhook",
        json=callback_update(f"ack:{alert.id}"),
        headers={"X-Telegram-Bot-Api-Secret-Token": WEBHOOK_SECRET},
    )

    steps = (
        (await session.execute(select(AlertStep).where(AlertStep.alert_id == alert.id)))
        .scalars()
        .all()
    )
    ack_steps = [s for s in steps if s.outcome == "acked"]
    assert len(ack_steps) == 1
    assert "Telegram" in (ack_steps[0].note or "")


async def test_buttons_removed_after_ack(
    app_and_client,
    session: AsyncSession,
    bot_transport: _FakeTelegramTransport,
    alert: Alert,
    admin,
) -> None:
    """Повторное нажатие «Принял» не должно плодить вторую запись в аудите."""
    _, client = app_and_client
    admin.telegram_chat_id = "555"
    await session.commit()

    await client.post(
        "/api/v1/telegram/webhook",
        json=callback_update(f"ack:{alert.id}"),
        headers={"X-Telegram-Bot-Api-Secret-Token": WEBHOOK_SECRET},
    )

    edits = bot_transport.of_method("editMessageText")
    assert len(edits) == 1
    assert edits[0]["payload"]["reply_markup"] == {"inline_keyboard": []}


async def test_spinner_is_stopped(
    app_and_client,
    session: AsyncSession,
    bot_transport: _FakeTelegramTransport,
    alert: Alert,
    admin,
) -> None:
    """Без answerCallbackQuery кнопка крутит часики полминуты."""
    _, client = app_and_client
    admin.telegram_chat_id = "555"
    await session.commit()

    await client.post(
        "/api/v1/telegram/webhook",
        json=callback_update(f"ack:{alert.id}"),
        headers={"X-Telegram-Bot-Api-Secret-Token": WEBHOOK_SECRET},
    )

    answers = bot_transport.of_method("answerCallbackQuery")
    assert len(answers) == 1
    assert answers[0]["payload"]["callback_query_id"] == "cbq_1"


async def test_ack_from_unlinked_chat_still_works(
    app_and_client, session: AsyncSession, alert: Alert, admin
) -> None:
    """Кнопка должна работать и из непривязанного чата.

    Иначе она бесполезна как резервный путь: админ переслал алерт коллеге, тот
    нажал «Принял» — и ничего. Действие применяется всегда, а в аудит пишется
    тот, кто нажал.
    """
    _, client = app_and_client

    response = await client.post(
        "/api/v1/telegram/webhook",
        json=callback_update(f"ack:{alert.id}", chat_id=999),
        headers={"X-Telegram-Bot-Api-Secret-Token": WEBHOOK_SECRET},
    )

    assert response.status_code == 200
    assert response.json()["result"] == "acked"

    await session.refresh(alert)
    assert alert.state == AlertState.ACKED


# ───────────────────────────── Snooze ─────────────────────────────


async def test_snooze_button_defers_alert(
    app_and_client, session: AsyncSession, alert: Alert, admin
) -> None:
    """Откладывание сдвигает эскалацию, но не подтверждает алерт."""
    _, client = app_and_client
    admin.telegram_chat_id = "555"
    await session.commit()

    response = await client.post(
        "/api/v1/telegram/webhook",
        json=callback_update(f"snooze:{telegram_route.SNOOZE_MINUTES}:{alert.id}"),
        headers={"X-Telegram-Bot-Api-Secret-Token": WEBHOOK_SECRET},
    )

    assert response.status_code == 200
    assert response.json()["result"] == "snoozed"

    await session.refresh(alert)
    assert alert.state == AlertState.TRIGGERED, "snooze не должен подтверждать"
    assert alert.snoozed_until is not None


async def test_snooze_uses_server_minutes_not_callback_data(
    app_and_client, session: AsyncSession, alert: Alert, admin
) -> None:
    """Подделанный callback_data не должен выпросить откладывание на год."""
    _, client = app_and_client
    admin.telegram_chat_id = "555"
    await session.commit()

    response = await client.post(
        "/api/v1/telegram/webhook",
        json=callback_update(f"snooze:99999:{alert.id}"),
        headers={"X-Telegram-Bot-Api-Secret-Token": WEBHOOK_SECRET},
    )

    assert response.status_code == 200
    await session.refresh(alert)

    delta = alert.snoozed_until.timestamp() - alert.last_seen_at.timestamp()
    assert delta < 60 * 60, (
        f"отложено на {delta / 60:.0f} мин вместо {telegram_route.SNOOZE_MINUTES}"
    )


async def test_snooze_limit_is_enforced(
    app_and_client, session: AsyncSession, bot_transport, alert: Alert, admin
) -> None:
    """Исчерпанный лимит — ответ пользователю в чат, а не сбой webhook."""
    from app.services.alerts import snooze_alert

    _, client = app_and_client
    admin.telegram_chat_id = "555"
    await session.commit()

    await snooze_alert(session, alert, 5)
    await snooze_alert(session, alert, 5)
    await session.commit()

    response = await client.post(
        "/api/v1/telegram/webhook",
        json=callback_update(f"snooze:15:{alert.id}"),
        headers={"X-Telegram-Bot-Api-Secret-Token": WEBHOOK_SECRET},
    )

    # Telegram повторяет запрос при не-2xx, и повторное откладывание только
    # испортило бы состояние. Поэтому отвечаем 200 с результатом rejected.
    assert response.status_code == 200
    assert response.json()["result"] == "rejected"

    answers = bot_transport.of_method("answerCallbackQuery")
    assert answers[-1]["payload"]["show_alert"] is True


# ───────────────────────────── Крайние случаи ─────────────────────────────


async def test_ack_after_resolve_is_rejected_gracefully(
    app_and_client, session: AsyncSession, bot_transport, alert: Alert, admin
) -> None:
    """Позднее нажатие: алерт уже закрыт, но кнопки должны исчезнуть."""
    from app.services.alerts import resolve_alert

    _, client = app_and_client
    admin.telegram_chat_id = "555"
    await resolve_alert(session, alert)
    await session.commit()

    response = await client.post(
        "/api/v1/telegram/webhook",
        json=callback_update(f"ack:{alert.id}"),
        headers={"X-Telegram-Bot-Api-Secret-Token": WEBHOOK_SECRET},
    )

    assert response.status_code == 200
    assert response.json()["result"] == "already_resolved"
    assert len(bot_transport.of_method("editMessageText")) == 1


async def test_unknown_alert_id_is_handled(
    app_and_client, bot_transport: _FakeTelegramTransport
) -> None:
    """Подделанный id не должен ронять webhook."""
    _, client = app_and_client

    response = await client.post(
        "/api/v1/telegram/webhook",
        json=callback_update("ack:alt_подделка"),
        headers={"X-Telegram-Bot-Api-Secret-Token": WEBHOOK_SECRET},
    )

    assert response.status_code == 200
    assert response.json()["handled"] is False


async def test_ordinary_message_is_ignored(
    app_and_client, bot_transport: _FakeTelegramTransport
) -> None:
    """Обычное сообщение или /start — не нажатие кнопки, и это не ошибка."""
    _, client = app_and_client

    response = await client.post(
        "/api/v1/telegram/webhook",
        json={"update_id": 1, "message": {"message_id": 1, "chat": {"id": 555}}},
        headers={"X-Telegram-Bot-Api-Secret-Token": WEBHOOK_SECRET},
    )

    assert response.status_code == 200
    assert response.json()["handled"] is False
    assert bot_transport.calls == []


# ───────────────────────────── Привязка и статус ─────────────────────────────


async def test_link_chat_requires_auth(app_and_client) -> None:
    """Без токена нельзя привязать чат — иначе можно было бы перехватить эскалацию."""
    _, client = app_and_client

    response = await client.post("/api/v1/telegram/link?chat_id=555")

    assert response.status_code == 401


async def test_link_chat_saves_id(app_and_client, session: AsyncSession, admin) -> None:
    _, client = app_and_client
    headers = await login(client)

    response = await client.post("/api/v1/telegram/link?chat_id=555", headers=headers)

    assert response.status_code == 200
    await session.refresh(admin)
    assert admin.telegram_chat_id == "555"


async def test_status_reports_reachable_chat(app_and_client, session, admin) -> None:
    """Настройки должны показывать, работает ли канал, а не только что он привязан."""
    _, client = app_and_client
    admin.telegram_chat_id = "555"
    await session.commit()
    headers = await login(client)

    response = await client.get("/api/v1/telegram/status", headers=headers)

    assert response.status_code == 200
    body = response.json()
    assert body["bot_configured"] is True
    assert body["chat_id"] == "555"
    assert body["chat_reachable"] is True


async def test_status_without_chat_is_null(app_and_client, admin) -> None:
    """Нет привязки — reachable неизвестно, а не «false»."""
    _, client = app_and_client
    headers = await login(client)

    body = (await client.get("/api/v1/telegram/status", headers=headers)).json()

    assert body["chat_id"] is None
    assert body["chat_reachable"] is None
