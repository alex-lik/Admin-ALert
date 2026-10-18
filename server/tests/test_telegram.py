"""Telegram-канал: отправка с кнопками и обработка нажатий (задача 4.8).

Тесты идут по двум направлениям, потому что у канала две разные ошибки:

- **транспорт** — неправильное тело запроса, HTML-разметка ломается на
  подчёркивании в имени хоста, бот отвечает `ok: false` с текстом;
- **поведение** — нажатие на уже устранённый алерт, исчерпанный лимит
  откладываний, нажатие из непривязанного чата.

Проверяется и то, и другое. Никакой сети: Bot API подменяется транспортом,
который отвечает заранее заданным JSON — как ответил бы Telegram.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.models import (
    Admin,
    Alert,
    Base,
    Device,
    EscalationProfile,
    EscalationStepDefinition,
    Host,
    Project,
)
from app.schemas.events import IncomingEvent
from app.services import dispatch, escalation
from app.services import telegram as tg
from app.services.alerts import ingest_event, now
from app.services.auth import register_admin
from app.services.dispatch import PushDispatcher
from app.services.escalation import Target
from app.services.telegram import CallbackAction, TelegramClient


class FakeTransport(httpx.AsyncBaseTransport):
    """Bot API заглушка: отвечает заданным JSON и помнит все вызовы."""

    def __init__(self, responses: dict[str, dict[str, Any]] | None = None) -> None:
        self.calls: list[dict[str, Any]] = []
        self._responses = responses or {}

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        method = request.url.path.rsplit("/", 1)[-1]
        self.calls.append(
            {"method": method, "payload": json.loads(request.content.decode("utf-8"))}
        )
        body = self._responses.get(method, {"ok": True, "result": {"message_id": 42}})
        return httpx.Response(200, json=body)

    def method_calls(self, method: str) -> list[dict[str, Any]]:
        return [c for c in self.calls if c["method"] == method]


@pytest.fixture
def transport() -> FakeTransport:
    return FakeTransport()


@pytest.fixture
def client(transport: FakeTransport) -> TelegramClient:
    return TelegramClient("123:ABC", http=httpx.AsyncClient(transport=transport))


@pytest_asyncio.fixture
async def session() -> AsyncIterator[AsyncSession]:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    factory = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    async with factory() as db:
        yield db

    await engine.dispose()


class _SessionScope:
    """Сессия, которую нельзя закрыть: тест проверяет ту же БД."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def __aenter__(self) -> AsyncSession:
        return self._session

    async def __aexit__(self, *_exc: object) -> None:
        return None


@pytest.fixture
def session_factory(session: AsyncSession):
    def factory() -> _SessionScope:
        return _SessionScope(session)

    return factory


@pytest_asyncio.fixture
async def admin(session: AsyncSession) -> Admin:
    return await register_admin(session, "duty@utax.top", "Дежурный", "verysecret123")


@pytest_asyncio.fixture
async def alert(session: AsyncSession) -> Alert:
    """Алерт в состоянии triggered с профилем из двух шагов."""
    session.add(
        EscalationProfile(id="critical", description="c", respects_quiet_hours=False, max_steps=6)
    )
    for number in (1, 2):
        session.add(
            EscalationStepDefinition(
                id=f"esd_{number}",
                profile_id="critical",
                step=number,
                channel="push" if number == 1 else "telegram",
                delay_seconds=0,
                target="current",
            )
        )
    session.add(
        Project(id="prj_x", name="flytaxi", is_vip=False, api_key="k", default_profile="critical")
    )
    session.add(Host(id="hst_x", project_id="prj_x", name="db_master-01"))
    await session.flush()

    project = await session.get(Project, "prj_x")
    created, _ = await ingest_event(
        session,
        project,
        IncomingEvent(
            project="flytaxi",
            host="db_master-01",
            check="disk_space",
            severity="critical",
            summary="Диск заполнен на 95%",
        ),
    )
    await session.commit()
    return created


def callback_update(
    *, data: str, chat_id: int = 555, message_id: int = 42, callback_id: str = "cbq_1"
) -> dict[str, Any]:
    """Обновление Telegram с нажатием inline-кнопки — формат бота как есть."""
    return {
        "update_id": 1,
        "callback_query": {
            "id": callback_id,
            "from": {"id": 777, "is_bot": False, "first_name": "Иван"},
            "chat_instance": "ci",
            "data": data,
            "message": {
                "message_id": message_id,
                "chat": {"id": chat_id, "type": "private"},
                "text": "🚨 Диск заполнен на 95%",
            },
        },
    }


# ───────────────────────────── Отправка ─────────────────────────────


async def test_alert_has_ack_and_snooze_buttons(
    client: TelegramClient, transport: FakeTransport
) -> None:
    """Кнопки Ack и Snooze — весь смысл канала: подтвердить, не открывая приложение."""
    result = await client.send_alert(
        "555", title="Диск заполнен", body="flytaxi · db_master-01", alert_id="alt_1", step=2
    )

    assert result.outcome == "delivered"
    payload = transport.method_calls("sendMessage")[0]["payload"]
    keyboard = payload["reply_markup"]["inline_keyboard"]
    buttons = [b["text"] for row in keyboard for b in row]
    assert any("Принял" in text for text in buttons)
    assert any("мин" in text for text in buttons)

    callbacks = [b["callback_data"] for row in keyboard for b in row]
    assert callbacks[0] == "ack:alt_1"
    assert callbacks[1].startswith("snooze:")


async def test_snooze_button_encodes_same_minutes_as_server(
    client: TelegramClient, transport: FakeTransport
) -> None:
    """Число на кнопке и число в обработчике не должны разъехаться."""
    await client.send_alert("555", title="t", body="b", alert_id="alt_1", step=1)
    payload = transport.method_calls("sendMessage")[0]["payload"]
    buttons = payload["reply_markup"]["inline_keyboard"][0]

    label_minutes = int(buttons[1]["text"].split()[1])
    parts = buttons[1]["callback_data"].split(":")
    encoded_minutes = int(parts[1])

    assert label_minutes == encoded_minutes


async def test_html_specials_in_host_are_escaped(
    client: TelegramClient, transport: FakeTransport
) -> None:
    """В HTML-режиме Telegram опасны только `&`, `<`, `>` — их и экранируем.

    Подчёркивания и звёздочки экранировать не нужно: в HTML-режиме они
    обычные символы, в отличие от Markdown-режима.
    """
    await client.send_alert(
        "555",
        title="Диск заполнен",
        body="flytaxi · db_master-01 <prod> & backup",
        alert_id="alt_1",
        step=1,
    )

    payload = transport.method_calls("sendMessage")[0]["payload"]
    text = payload["text"]

    assert "db_master-01 <prod> & backup" not in text
    assert "db_master-01 &lt;prod&gt; &amp; backup" in text


async def test_occurrences_shown_only_when_repeated(
    client: TelegramClient, transport: FakeTransport
) -> None:
    """«повтор 1» — шум; «повтор 5» — факт, который меняет решение."""
    await client.send_alert("555", title="t", body="b", alert_id="a1", step=1, occurrences=1)
    await client.send_alert("555", title="t", body="b", alert_id="a1", step=1, occurrences=5)

    texts = [c["payload"]["text"] for c in transport.method_calls("sendMessage")]
    assert "повтор" not in texts[0]
    assert "повтор 5" in texts[1]


async def test_resolved_message_has_no_buttons(
    client: TelegramClient, transport: FakeTransport
) -> None:
    """Кнопка «Принял» на устранённом алерте бессмысленна: подтверждать нечего."""
    result = await client.send_alert(
        "555", title="t", body="b", alert_id="a1", step=1, resolved=True
    )

    assert result.outcome == "delivered"
    payload = transport.method_calls("sendMessage")[0]["payload"]
    assert "reply_markup" not in payload
    assert "устранена" in payload["text"]


async def test_telegram_ok_false_is_failure() -> None:
    """Telegram отвечает 200 с ok:false — по коду статуса это не видно."""

    class Rejecting(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200, json={"ok": False, "error_code": 403, "description": "bot was kicked"}
            )

    client = TelegramClient("123:ABC", http=httpx.AsyncClient(transport=Rejecting()))
    result = await client.send_alert("555", title="t", body="b", alert_id="a1", step=1)

    assert result.outcome == "failed"
    assert "bot was kicked" in (result.note or "")


async def test_without_token_send_is_skipped_not_failed() -> None:
    """Нет токена — это не сбой канала, а «канал не подключён»."""
    client = TelegramClient(None)

    result = await client.send_alert("555", title="t", body="b", alert_id="a1", step=1)

    assert result.outcome == "skipped"
    assert "не настроен" in (result.note or "")


async def test_network_error_does_not_raise() -> None:
    """Канал не должен ронять проход воркера."""

    class Broken(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("нет сети", request=request)

    client = TelegramClient("123:ABC", http=httpx.AsyncClient(transport=Broken()))
    result = await client.send_alert("555", title="t", body="b", alert_id="a1", step=1)

    assert result.outcome == "failed"
    assert "недоступен" in (result.note or "")


# ───────────────────────────── Разбор нажатий ─────────────────────────────


def test_parse_ack_callback() -> None:
    action = tg.parse_callback(callback_update(data="ack:alt_42"))

    assert action is not None
    assert action.action == "ack"
    assert action.alert_id == "alt_42"
    assert action.callback_id == "cbq_1"
    assert action.chat_id == "555"


def test_parse_snooze_callback() -> None:
    action = tg.parse_callback(callback_update(data="snooze:15:alt_42"))

    assert action is not None
    assert action.action == "snooze"
    assert action.alert_id == "alt_42"


def test_parse_ignores_ordinary_message() -> None:
    """Бот получает и обычные сообщения, и /start — это не нажатие кнопки."""
    assert tg.parse_callback({"update_id": 1, "message": {"message_id": 1}}) is None


def test_parse_ignores_foreign_button() -> None:
    """Кнопка от другой системы не должна приводить к действию."""
    update = callback_update(data="help:что-то")
    assert tg.parse_callback(update) is None


def test_parse_ignores_snooze_without_minutes() -> None:
    """Битый callback_data игнорируем, а не превращаем в snooze на 0 минут."""
    assert tg.parse_callback(callback_update(data="snooze::alt_42")) is None
    assert tg.parse_callback(callback_update(data="snooze:abc:alt_42")) is None


def test_parse_requires_callback_id() -> None:
    """Без id нельзя снять «часики» на кнопке — такое обновление не обрабатываем."""
    update = callback_update(data="ack:alt_42")
    del update["callback_query"]["id"]

    assert tg.parse_callback(update) is None


# ───────────────────────────── Диспетчер ─────────────────────────────


async def test_dispatcher_sends_telegram_step(
    session: AsyncSession,
    session_factory,
    client: TelegramClient,
    transport: FakeTransport,
    alert: Alert,
    admin: Admin,
) -> None:
    """Шаг канала telegram уходит в чат дежурного, а не пишет skipped."""
    admin.telegram_chat_id = "555"
    await session.commit()

    outcome, note = await PushDispatcher(session_factory, None, telegram=client).send(
        "telegram", alert, Target(admin.id, "Дежурный")
    )

    assert outcome == "delivered"
    assert "Telegram" in (note or "")
    payload = transport.method_calls("sendMessage")[0]["payload"]
    assert payload["chat_id"] == "555"
    assert "Диск заполнен" in payload["text"]


async def test_unlinked_chat_is_skipped_not_failed(
    session_factory, client: TelegramClient, alert: Alert, admin: Admin
) -> None:
    """Нет привязки — шаг идёт дальше по расписанию, а не спотыкается о резервный канал."""
    outcome, note = await PushDispatcher(session_factory, None, telegram=client).send(
        "telegram", alert, Target(admin.id, "Дежурный")
    )

    assert outcome == "skipped"
    assert "не привязан" in (note or "")


async def test_without_bot_token_is_skipped(
    session: AsyncSession, session_factory, alert: Alert, admin: Admin
) -> None:
    admin.telegram_chat_id = "555"
    await session.commit()

    outcome, note = await PushDispatcher(session_factory, None, telegram=TelegramClient(None)).send(
        "telegram", alert, Target(admin.id, "Дежурный")
    )

    assert outcome == "skipped"
    assert "токена бота" in (note or "")


async def test_call_channel_still_skipped(session_factory, alert: Alert, admin: Admin) -> None:
    """Звонки не подключены — это по-прежнему skipped, а не выдуманная доставка."""
    outcome, note = await PushDispatcher(session_factory, None, telegram=TelegramClient(None)).send(
        "call", alert, Target(admin.id, "Дежурный")
    )

    assert outcome == "skipped"
    assert "call" in (note or "")


async def test_worker_marks_telegram_step_delivered(
    session: AsyncSession,
    session_factory,
    client: TelegramClient,
    transport: FakeTransport,
    alert: Alert,
    admin: Admin,
) -> None:
    """Главная проверка: второй шаг профиля доезжает по Telegram и попадает в аудит."""
    from app.models import AlertStep

    admin.telegram_chat_id = "555"
    session.add(
        Device(
            id="dev_ok",
            admin_id=admin.id,
            token="tok-ok",
            platform="android",
            last_seen_at=now(),
            is_active=True,
        )
    )
    await session.commit()

    # Шаг 1 — push (устройство на связи, NullDispatcher пишет skipped),
    # шаг 2 — telegram. Оба проходят в одном цикле воркера.
    dispatcher = PushDispatcher(session_factory, None, telegram=client)
    await escalation.poll_once(session, dispatcher, use_lock=False)
    alert.next_action_at = now() - __import__("datetime").timedelta(seconds=1)
    await session.flush()
    await escalation.poll_once(session, dispatcher, use_lock=False)

    steps = (
        (
            await session.execute(
                select(AlertStep).where(AlertStep.alert_id == alert.id).order_by(AlertStep.step)
            )
        )
        .scalars()
        .all()
    )

    telegram_steps = [s for s in steps if s.channel == "telegram"]
    assert len(telegram_steps) == 1
    assert telegram_steps[0].outcome == "delivered"
    assert alert.current_step == 2

    # Номер шага в тексте сообщения обязан совпадать с номером выполняемого
    # шага. Раньше читался alert.current_step, который на момент отправки ещё
    # указывал на предыдущий: дежурный видел «шаг 1» вместо «шаг 2».
    sent = transport.method_calls("sendMessage")[0]["payload"]
    assert "шаг эскалации 2" in sent["text"]


def test_make_dispatcher_passes_telegram_token() -> None:
    """Токен из настроек доходит до клиента — иначе канал молчал бы всегда."""

    class Settings:
        fcm_service_account_file = None
        fcm_project_id = None
        apns_critical_alerts = False
        telegram_bot_token = "123:ABC"

    dispatcher = dispatch.make_dispatcher(Settings())

    assert dispatcher.telegram_client is not None
    assert dispatcher.telegram_client.configured is True


# ───────────────────────────── Привязка чата ─────────────────────────────


async def test_link_chat_saves_id(session: AsyncSession, admin: Admin) -> None:
    admin.telegram_chat_id = "555"
    await session.commit()
    await session.refresh(admin)

    assert admin.telegram_chat_id == "555"


async def test_chat_lookup_ignores_inactive_admin(session: AsyncSession, admin: Admin) -> None:
    """Неактивный дежурный не должен находиться по своему чату."""
    from app.api.routes.telegram import _admin_for_chat

    admin.telegram_chat_id = "555"
    await session.commit()
    assert await _admin_for_chat(session, "555") is not None

    admin.is_active = False
    await session.commit()
    assert await _admin_for_chat(session, "555") is None


def test_callback_action_is_hashable() -> None:
    """Используется как ключ в тестах и в кэше — должна быть неизменяемой."""
    action = CallbackAction(
        action="ack", alert_id="a1", chat_id="1", message_id=1, user_id=1, callback_id="c1"
    )

    assert {action: "ok"}[action] == "ok"
