"""Тесты отправки push (план, задачи 2.16–2.19, 2.6).

Сеть не используется: FCM-транспорт подменяется `httpx.MockTransport`, ключи
сервисного аккаунта — сгенерированной парой RSA. Это проверяет ровно то, что
важно и не зависит от Google: правильное тело запроса, приоритет high, разбор
ошибок и деактивацию мёртвых токенов.
"""

from __future__ import annotations

import base64
import json
from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest
import pytest_asyncio
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.models import (
    Alert,
    Base,
    Device,
    EscalationProfile,
    EscalationStepDefinition,
    Host,
    Project,
)
from app.models.enums import AlertState, Severity
from app.schemas.events import IncomingEvent
from app.services import dispatch, escalation, push
from app.services.alerts import now
from app.services.auth import register_admin
from app.services.dispatch import PushDispatcher
from app.services.escalation import Target
from app.services.push import FcmClient, PushNotConfigured, ServiceAccount

# ───────────────────────────── Подготовка ─────────────────────────────


class _RecordTransport(httpx.AsyncBaseTransport):
    """Транспорт, который выполняет обработчик и остаётся асинхронным.

    `httpx.MockTransport` умеет так же, но он же различает ответы по URL, из-за
    чего в тестах с двумя эндпоинтами (обмен токена и отправка) приходилось
    подменять клиент целиком. Здесь состояние теста — обычный словарь.
    """

    def __init__(self, handler: Any) -> None:
        self._handler = handler

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        return self._handler(request)


@pytest.fixture(scope="module")
def private_key() -> str:
    """RSA-ключ для подписи JWT. Модульный: генерация 2048 бит небыстрая."""
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode("ascii")


@pytest.fixture
def account(private_key: str) -> ServiceAccount:
    return ServiceAccount(
        project_id="flytaxi-alerts",
        client_email="push@flytaxi-alerts.iam.gserviceaccount.com",
        private_key=private_key,
        token_uri="https://oauth2.googleapis.com/token",
    )


@pytest.fixture
def sent_messages() -> list[dict[str, Any]]:
    """Тела запросов в FCM — их и будем проверять."""
    return []


@pytest.fixture
def mock_responses(sent_messages: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "send_status": 200,
        "send_body": {"name": "projects/flytaxi-alerts/messages/1"},
    }


@pytest_asyncio.fixture
async def fcm(
    account: ServiceAccount,
    sent_messages: list[dict[str, Any]],
    mock_responses: dict[str, Any],
) -> FcmClient:
    """FCM-клиент на поддельном транспорте.

    Ответ на обмен JWT и на саму отправку разводятся по URL: иначе тесты на
    тело сообщения ловили бы ответ от токен-эндпоинта.
    """

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/token":
            return httpx.Response(200, json={"access_token": "ya29.test", "expires_in": 3600})

        sent_messages.append(json.loads(request.content.decode("utf-8")))
        return httpx.Response(mock_responses["send_status"], json=mock_responses["send_body"])

    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return FcmClient(account, http=http)


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
    """Сессия, которую нельзя закрыть.

    Диспетчер в бою получает свежую сессию из `SessionLocal` и закрывает её сам.
    В тесте нужна та же самая сессия (чтобы видеть её изменения), поэтому
    закрытие подавляется — иначе первый же `async with` убил бы её.
    """

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
async def admin(session: AsyncSession):
    return await register_admin(session, "duty@utax.top", "Дежурный", "verysecret123")


@pytest_asyncio.fixture
async def alert(session: AsyncSession, admin) -> Alert:
    """Алерт с профилем critical и заполненными связями — как в бою."""
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
    # Не VIP: иначе resolve_profile_id выбрал бы critical-vip, а профиль с таким
    # именем в тесте не заведён — и алерт остался бы без шагов.
    session.add(
        Project(id="prj_x", name="flytaxi", is_vip=False, api_key="k", default_profile="critical")
    )
    session.add(Host(id="hst_x", project_id="prj_x", name="db-master-01"))
    await session.flush()

    project = await session.get(Project, "prj_x")
    event = IncomingEvent(
        project="flytaxi",
        host="db-master-01",
        check="disk_space",
        severity=Severity.CRITICAL,
        summary="Диск заполнен на 95%",
    )
    from app.services.alerts import ingest_event

    created, _ = await ingest_event(session, project, event)
    return created


def add_device(
    session: AsyncSession, admin_id: str, token: str, platform: str = "android"
) -> Device:
    device = Device(
        id=f"dev_{token[-6:]}",
        admin_id=admin_id,
        token=token,
        platform=platform,
        last_seen_at=now(),
        is_active=True,
    )
    session.add(device)
    return device


# ───────────────────────────── Транспорт ─────────────────────────────


def test_jwt_is_signed_and_scoped(fcm: FcmClient, account: ServiceAccount) -> None:
    """Google требует именно RS256-JWT с правильным scope и audience.

    Если scope или aud съедут, ошибка придёт от Google как 400 с невнятным
    текстом — поэтому проверяем тут, где можно поймать сразу.
    """
    assertion = fcm._tokens._make_assertion(1_700_000_000)  # noqa: SLF001 — проверяем внутреннее

    header_b64, claims_b64, _ = assertion.split(".")
    header = json.loads(base64.urlsafe_b64decode(header_b64 + "=="))
    claims = json.loads(base64.urlsafe_b64decode(claims_b64 + "=="))

    assert header == {"alg": "RS256", "typ": "JWT"}
    assert claims["iss"] == account.client_email
    assert claims["scope"] == push.SCOPE
    assert claims["aud"] == account.token_uri
    assert claims["exp"] - claims["iat"] == push.TOKEN_TTL_SECONDS


async def test_data_only_message_with_high_priority(
    fcm: FcmClient, sent_messages: list[dict[str, Any]]
) -> None:
    """Data-only + priority high: иначе Android в фоне сообщение игнорирует."""
    result = await fcm.send_data("token-1", {"alert_id": "alt_1"}, collapse_key="flytaxi:disk")

    assert result.outcome == "delivered"
    message = sent_messages[0]["message"]
    assert "notification" not in message, "своё уведомление строит приложение"
    assert message["android"]["priority"] == "high"
    assert message["android"]["collapse_key"] == "flytaxi:disk"
    assert message["android"]["ttl"].endswith("s")


async def test_apns_block_only_for_ios(fcm: FcmClient, sent_messages: list[dict[str, Any]]) -> None:
    """Без apns_alert блока APNs нет — чистый Android-вызов."""
    await fcm.send_data("token-1", {"a": "b"})
    assert "apns" not in sent_messages[0]["message"]


async def test_apns_critical_sound_requires_flag(
    fcm: FcmClient, sent_messages: list[dict[str, Any]]
) -> None:
    """`critical: 1` без entitlement от Apple приводит к BadPriority — не шлём по умолчанию."""
    await fcm.send_data("token-1", {"a": "b"}, apns_alert={"title": "t", "body": "b"})
    aps = sent_messages[0]["message"]["apns"]["payload"]["aps"]
    assert aps["sound"] == "default"
    assert aps["interruption-level"] == "time-sensitive"

    await fcm.send_data(
        "token-2", {"a": "b"}, apns_alert={"title": "t", "body": "b"}, apns_critical=True
    )
    aps = sent_messages[1]["message"]["apns"]["payload"]["aps"]
    assert aps["sound"] == {"critical": 1, "name": "default", "volume": 1.0}


@pytest.mark.parametrize(
    ("error_code", "expect_invalid"),
    [
        ("UNREGISTERED", True),
        ("INVALID_ARGUMENT", True),
        ("SENDER_ID_MISMATCH", True),
        ("QUOTA_EXCEEDED", False),
        ("UNAVAILABLE", False),
        ("INTERNAL", False),
    ],
)
async def test_error_classification(
    fcm: FcmClient,
    mock_responses: dict[str, Any],
    error_code: str,
    expect_invalid: bool,
) -> None:
    """Разделение «токен мёртв» и «сеть легла» — главное в разборе ошибок."""
    mock_responses["send_status"] = 400
    mock_responses["send_body"] = {
        "error": {
            "code": 400,
            "status": "INVALID_ARGUMENT",
            "message": "test",
            "details": [
                {
                    "@type": "type.googleapis.com/google.firebase.fcm.v1.FcmError",
                    "errorCode": error_code,
                }
            ],
        }
    }

    result = await fcm.send_data("token-1", {"a": "b"})

    assert result.outcome == ("invalid_token" if expect_invalid else "failed")
    assert result.token_invalid is expect_invalid


async def test_404_without_error_code_is_dead_token(
    fcm: FcmClient, mock_responses: dict[str, Any]
) -> None:
    """FCM отвечает 404 и без errorCode, когда токена просто нет."""
    mock_responses["send_status"] = 404
    mock_responses["send_body"] = {"error": {"code": 404, "status": "NOT_FOUND", "message": ""}}

    result = await fcm.send_data("token-1", {"a": "b"})

    assert result.outcome == "invalid_token"


async def test_server_error_is_retryable(fcm: FcmClient, mock_responses: dict[str, Any]) -> None:
    """500 у Google — временная беда, токен при этом жив."""
    mock_responses["send_status"] = 503
    mock_responses["send_body"] = {
        "error": {"code": 503, "status": "UNAVAILABLE", "message": "down"}
    }

    result = await fcm.send_data("token-1", {"a": "b"})

    assert result.outcome == "failed"
    assert result.token_invalid is False


async def test_network_error_does_not_raise(account: ServiceAccount) -> None:
    """Падение сети в пути запроса алерта не должно ронять HTTP-вызов."""

    def boom(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("нет сети", request=request)

    http = httpx.AsyncClient(transport=httpx.MockTransport(boom))
    client = FcmClient(account, http=http)

    result = await client.send_data("token-1", {"a": "b"})

    assert result.outcome == "failed"
    assert "недоступен" in (result.note or "")


def test_service_account_requires_project_id(tmp_path) -> None:
    """Файл без project_id — это ошибка конфигурации, а не молчание."""
    path = tmp_path / "sa.json"
    path.write_text(json.dumps({"client_email": "e", "private_key": "k"}), encoding="utf-8")

    with pytest.raises(PushNotConfigured):
        ServiceAccount.from_file(path)


# ───────────────────────────── Диспетчер ─────────────────────────────


async def test_dispatcher_without_fcm_is_honest(alert: Alert) -> None:
    """Нет ключей Firebase — шаг помечается skipped, а не delivered."""
    dispatcher = PushDispatcher(lambda: None, None)  # noqa: ARG005 — сессия не нужна

    outcome, note = await dispatcher.send("push", alert, Target("adm_1", "Дежурный"))

    assert outcome == "skipped"
    assert "FCM не настроен" in (note or "")


async def test_unconnected_channels_are_skipped(alert: Alert, fcm: FcmClient) -> None:
    """Звонок ещё не подключён — честный skipped, не выдуманная доставка.

    Telegram проверяется отдельно, в tests/test_telegram.py: он уже подключён.
    """
    dispatcher = PushDispatcher(lambda: None, fcm)

    outcome, note = await dispatcher.send("call", alert, Target("adm_1", "Дежурный"))

    assert outcome == "skipped"
    assert "call" in (note or "")


async def test_push_reaches_every_device(
    session: AsyncSession,
    session_factory,
    fcm: FcmClient,
    sent_messages: list[dict[str, Any]],
    alert: Alert,
    admin,
) -> None:
    """Дежурный может быть не у того аппарата, что под рукой — шлём во все."""
    add_device(session, admin.id, "token-android-1")
    add_device(session, admin.id, "token-android-2")
    await session.commit()

    dispatcher = PushDispatcher(session_factory, fcm)
    outcome, note = await dispatcher.send("push", alert, Target(admin.id, "Дежурный"))

    assert outcome == "delivered"
    assert len(sent_messages) == 2
    assert {m["message"]["token"] for m in sent_messages} == {
        "token-android-1",
        "token-android-2",
    }
    assert "2 устройств" in (note or "")


async def test_payload_carries_alert_context(
    session: AsyncSession, session_factory, fcm: FcmClient, sent_messages, alert: Alert, admin
) -> None:
    """Приложение строит уведомление само — значит всё нужное едет в data."""
    add_device(session, admin.id, "token-android-1")
    await session.commit()

    await PushDispatcher(session_factory, fcm).send("push", alert, Target(admin.id, "Д"))

    data = sent_messages[0]["message"]["data"]
    assert data["alert_id"] == alert.id
    assert data["channel"] == "critical", "канал выбирается по профилю эскалации"
    assert data["project"] == "flytaxi"
    assert data["host"] == "db-master-01"
    assert data["summary"] == "Диск заполнен на 95%"
    assert data["type"] == "alert"


async def test_vip_profile_selects_vip_channel(
    session: AsyncSession, session_factory, fcm: FcmClient, sent_messages, alert: Alert, admin
) -> None:
    """VIP-профиль обязан звать в самый агрессивный канал, а не в critical."""
    alert.profile_id = "critical-vip"
    add_device(session, admin.id, "token-android-1")
    await session.commit()

    await PushDispatcher(session_factory, fcm).send("push", alert, Target(admin.id, "Д"))

    assert sent_messages[0]["message"]["data"]["channel"] == "critical-vip"


async def test_unknown_profile_falls_back_to_severity(alert: Alert) -> None:
    """Неизвестный профиль не должен уводить алерт в канал warning."""
    alert.profile_id = "local-disk"

    assert dispatch.notification_channel(alert) == "critical"


async def test_dead_token_disables_device(
    session: AsyncSession,
    session_factory,
    fcm: FcmClient,
    mock_responses: dict[str, Any],
    alert: Alert,
    admin,
) -> None:
    """Задача 2.19: токен мёртв — устройство гасится, чтобы не пейджить в пустоту."""
    device = add_device(session, admin.id, "token-dead-999999")
    await session.commit()

    mock_responses["send_status"] = 404
    mock_responses["send_body"] = {
        "error": {"code": 404, "status": "NOT_FOUND", "message": "Requested entity was not found."}
    }

    outcome, note = await PushDispatcher(session_factory, fcm).send(
        "push", alert, Target(admin.id, "Д")
    )

    assert outcome == "failed", "доставки нет — шаг не должен числиться успешным"
    assert "мёртвым токеном" in (note or "")

    await session.refresh(device)
    assert device.is_active is False


async def test_one_dead_device_among_healthy(
    session: AsyncSession,
    session_factory,
    fcm: FcmClient,
    mock_responses: dict[str, Any],
    sent_messages: list[dict[str, Any]],
    alert: Alert,
    admin,
) -> None:
    """Один сломанный токен не обнуляет доставку на остальные устройства."""
    dead = add_device(session, admin.id, "token-dead-000001")
    add_device(session, admin.id, "token-good-000002")
    await session.commit()

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/token":
            return httpx.Response(200, json={"access_token": "ya29.test", "expires_in": 3600})
        sent_messages.append(json.loads(request.content.decode("utf-8")))
        # Мёртвый токен узнаём по содержимому запроса: адресат один, различаются
        # только токены внутри.
        if b"token-dead" in request.content:
            return httpx.Response(
                404,
                json={"error": {"code": 404, "status": "NOT_FOUND", "message": "no such token"}},
            )
        return httpx.Response(200, json={"name": "ok"})

    fcm._http = httpx.AsyncClient(transport=_RecordTransport(handler))  # noqa: SLF001

    outcome, note = await PushDispatcher(session_factory, fcm).send(
        "push", alert, Target(admin.id, "Д")
    )

    assert outcome == "delivered"
    assert "1 из 2 устройств" in (note or "")
    await session.refresh(dead)
    assert dead.is_active is False


async def test_no_devices_is_failed_not_skipped(
    session_factory, fcm: FcmClient, alert: Alert, admin
) -> None:
    """Нет устройств — это провал шага, а не «нечего делать»: эскалация идёт дальше."""
    outcome, note = await PushDispatcher(session_factory, fcm).send(
        "push", alert, Target(admin.id, "Д")
    )

    assert outcome == "failed"
    assert "нет активных устройств" in (note or "")


async def test_resolved_notification_says_resolved(
    session: AsyncSession, session_factory, fcm: FcmClient, sent_messages, alert: Alert, admin
) -> None:
    """Задача 2.6: закрытый алерт должен явно сказать «устранено», а не «алерт»."""
    add_device(session, admin.id, "token-android-1")
    await session.commit()

    alert.state = AlertState.RESOLVED
    await session.commit()

    outcome = await dispatch.notify_resolved(PushDispatcher(session_factory, fcm), alert.id)

    assert outcome == "delivered"
    assert sent_messages[0]["message"]["data"]["type"] == "resolved"


async def test_resolved_notification_never_raises(session_factory, alert: Alert, admin) -> None:
    """Упавшая отправка не должна превращать успешный resolve в 500."""

    class Broken:
        configured = True
        session_factory = None

        async def send_resolved(self, *_a, **_kw):
            raise RuntimeError("Google лёг")

    broken = Broken()
    broken.session_factory = session_factory

    async def fake_load(_alert_id: str):
        return alert

    broken.load_for_notify = fake_load

    assert await dispatch.notify_resolved(broken, alert.id) == "skipped"


# ───────────────────────────── Интеграция с воркером ─────────────────────────────


async def test_worker_step_is_marked_delivered(
    session: AsyncSession, session_factory, fcm: FcmClient, sent_messages, alert: Alert, admin
) -> None:
    """Главная проверка: воркер перестаёт писать skipped, когда push доставлен."""
    from app.models import AlertStep

    add_device(session, admin.id, "token-android-1")
    await session.commit()

    processed = await escalation.poll_once(
        session, PushDispatcher(session_factory, fcm), use_lock=False
    )

    assert processed == 1
    assert alert.state == AlertState.NOTIFYING

    steps = (
        (await session.execute(select(AlertStep).where(AlertStep.alert_id == alert.id)))
        .scalars()
        .all()
    )
    assert len(steps) == 1
    assert steps[0].outcome == "delivered"
    assert len(sent_messages) == 1


def test_make_dispatcher_without_keys_is_not_configured() -> None:
    """Отсутствие ключей не должно ронять воркер на старте."""

    class Settings:
        fcm_service_account_file = None
        fcm_project_id = None
        apns_critical_alerts = False

    dispatcher = dispatch.make_dispatcher(Settings())

    assert dispatcher.configured is False


def test_make_dispatcher_with_broken_file_logs_and_continues(tmp_path, caplog) -> None:
    """Битый файл ключей — ошибка конфигурации, воркер продолжает работать."""
    path = tmp_path / "sa.json"
    path.write_text("{не json}", encoding="utf-8")

    class Settings:
        fcm_service_account_file = str(path)
        fcm_project_id = None
        apns_critical_alerts = False

    dispatcher = dispatch.make_dispatcher(Settings())

    assert dispatcher.configured is False
