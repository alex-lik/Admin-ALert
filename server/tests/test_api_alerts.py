"""Тесты HTTP-слоя: вход, список алертов, ack / snooze (задачи 2.2, 2.14, 5.2)."""

from collections.abc import AsyncIterator
from typing import Any

import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.db import get_session
from app.main import create_app
from app.models import Alert, AlertState, Base, EscalationProfile, Project
from app.schemas.events import IncomingEvent
from app.services.alerts import ingest_event
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
async def project(session: AsyncSession) -> Project:
    for profile_id in ("warning", "critical", "critical-vip"):
        session.add(
            EscalationProfile(id=profile_id, description=profile_id, respects_quiet_hours=True)
        )

    proj = Project(id="prj_api", name="api-project", is_vip=False, api_key="key-api")
    session.add(proj)
    await session.flush()
    return proj


@pytest_asyncio.fixture
async def admin(session: AsyncSession):
    return await register_admin(session, ADMIN_EMAIL, "Иван Петров", ADMIN_PASSWORD)


@pytest_asyncio.fixture
async def client(session: AsyncSession) -> AsyncIterator[AsyncClient]:
    """Клиент с подменённой сессией БД — чтобы тесты шли против той же схемы."""
    app = create_app()

    async def _override() -> AsyncIterator[AsyncSession]:
        yield session

    app.dependency_overrides[get_session] = _override

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as http:
        yield http


async def login(client: AsyncClient) -> dict[str, Any]:
    response = await client.post(
        "/api/v1/auth/login",
        json={"email": ADMIN_EMAIL, "password": ADMIN_PASSWORD},
    )
    assert response.status_code == 200, response.text
    return response.json()


def auth_headers(tokens: dict[str, Any]) -> dict[str, str]:
    return {"Authorization": f"Bearer {tokens['access_token']}"}


async def make_alert(session: AsyncSession, project: Project, **overrides: Any) -> Alert:
    payload: dict[str, Any] = {
        "project": project.name,
        "host": "host-01",
        "check": "disk_space",
        "severity": "critical",
        "summary": "Диск заполнен",
    }
    payload.update(overrides)
    alert, _ = await ingest_event(session, project, IncomingEvent(**payload))
    return alert


# ───────────────────────────── Вход ─────────────────────────────


async def test_login_returns_token_pair(client: AsyncClient, admin) -> None:
    body = await login(client)

    assert body["token_type"] == "bearer"
    assert body["access_token"] and body["refresh_token"]
    assert body["admin"]["email"] == ADMIN_EMAIL


async def test_login_rejects_wrong_password(client: AsyncClient, admin) -> None:
    response = await client.post(
        "/api/v1/auth/login",
        json={"email": ADMIN_EMAIL, "password": "неправильный"},
    )
    assert response.status_code == 401


async def test_login_rejects_unknown_email(client: AsyncClient, admin) -> None:
    """Ответ должен быть тем же, что и при неверном пароле — иначе перебором
    выясняется, какие email зарегистрированы."""
    unknown = await client.post(
        "/api/v1/auth/login",
        json={"email": "nobody@utax.top", "password": ADMIN_PASSWORD},
    )
    wrong = await client.post(
        "/api/v1/auth/login",
        json={"email": ADMIN_EMAIL, "password": "неправильный"},
    )

    assert unknown.status_code == wrong.status_code == 401
    assert unknown.json()["detail"] == wrong.json()["detail"]


async def test_alerts_require_authentication(client: AsyncClient, session, project) -> None:
    response = await client.get("/api/v1/alerts")
    assert response.status_code == 401


async def test_refresh_returns_new_tokens(client: AsyncClient, admin) -> None:
    tokens = await login(client)

    response = await client.post(
        "/api/v1/auth/refresh", json={"refresh_token": tokens["refresh_token"]}
    )
    assert response.status_code == 200
    assert response.json()["access_token"] != tokens["access_token"]


async def test_tokens_are_unique_per_issue(client: AsyncClient, admin) -> None:
    """Каждый выпуск даёт уникальный токен.

    Без jti в payload подпись детерминирована: два входа в одну секунду дали бы
    байт-в-байт одинаковый access-токен, и его нельзя было бы ни отозвать,
    ни отличить от предыдущего выпуска.
    """
    first = await login(client)
    second = await login(client)

    assert first["access_token"] != second["access_token"]
    assert first["refresh_token"] != second["refresh_token"]


async def test_access_token_cannot_be_used_as_refresh(client: AsyncClient, admin) -> None:
    """Путать типы токенов нельзя: иначе refresh продлевал бы сессию вечно."""
    tokens = await login(client)

    response = await client.post(
        "/api/v1/auth/refresh", json={"refresh_token": tokens["access_token"]}
    )
    assert response.status_code == 401


# ───────────────────────────── События ─────────────────────────────


async def test_post_events_requires_api_key(client: AsyncClient) -> None:
    payload = {
        "project": "api-project",
        "host": "h",
        "check": "c",
        "severity": "warning",
        "summary": "s",
    }

    assert (await client.post("/api/v1/events", json=payload)).status_code == 401
    assert (
        await client.post("/api/v1/events", json=payload, headers={"X-API-Key": "wrong"})
    ).status_code == 401


async def test_post_events_accepted_and_deduplicated(
    client: AsyncClient, session: AsyncSession, project: Project
) -> None:
    payload = {
        "project": project.name,
        "host": "host-01",
        "check": "disk_space",
        "severity": "critical",
        "summary": "Диск заполнен",
    }

    first = await client.post("/api/v1/events", json=payload, headers={"X-API-Key": "key-api"})
    assert first.status_code == 202
    assert first.json()["duplicate"] is False
    assert first.json()["occurrences"] == 1

    second = await client.post("/api/v1/events", json=payload, headers={"X-API-Key": "key-api"})
    assert second.status_code == 202
    assert second.json()["duplicate"] is True
    assert second.json()["occurrences"] == 2
    assert second.json()["alert_id"] == first.json()["alert_id"]


# ───────────────────────────── Список и ack ─────────────────────────────


async def test_alerts_list_shape(
    client: AsyncClient, session: AsyncSession, project: Project, admin
) -> None:
    await make_alert(session, project)
    tokens = await login(client)

    response = await client.get("/api/v1/alerts", headers=auth_headers(tokens))
    assert response.status_code == 200

    body = response.json()
    assert body["total"] == 1
    assert body["active_count"] == 1
    assert body["critical_count"] == 1

    item = body["items"][0]
    # Набор полей должен совпадать с тем, что читает клиент
    # (client/src/types/alert.ts).
    for field in ("id", "project", "host", "check", "severity", "state", "summary", "dedup_key"):
        assert field in item, f"в ответе нет поля {field}"


async def test_alerts_only_active_filter(
    client: AsyncClient, session: AsyncSession, project: Project, admin
) -> None:
    await make_alert(session, project, host="host-01")
    await make_alert(session, project, host="host-02")
    tokens = await login(client)

    response = await client.get("/api/v1/alerts?only_active=true", headers=auth_headers(tokens))
    assert response.json()["total"] == 2


async def test_ack_stops_escalation_and_returns_what_stopped(
    client: AsyncClient, session: AsyncSession, project: Project, admin
) -> None:
    """Задача 2.15: после ack останавливается всё, и админ видит, что именно."""
    alert = await make_alert(session, project)
    alert.state = AlertState.NOTIFYING
    alert.current_step = 2
    await session.flush()

    tokens = await login(client)
    response = await client.post(
        f"/api/v1/alerts/{alert.id}/ack",
        json={"comment": "почистил логи"},
        headers=auth_headers(tokens),
    )

    assert response.status_code == 200
    body = response.json()
    assert body["state"] == "acked"
    assert body["acked_by"] == "Иван Петров"
    assert body["stopped"], "клиенту нужно знать, что именно остановилось"
    # Таймер обязан быть погашен: иначе воркер выполнит ещё шаг по закрытому алерту.
    assert alert.next_action_at is None


async def test_ack_is_persisted_not_just_returned(
    client: AsyncClient, session: AsyncSession, project: Project, admin
) -> None:
    """Регрессия: маршрут возвращал acked, но не коммитил.

    Сессия закрывалась с откатом, в базе оставался triggered, и эскалация
    продолжалась. Ответ выглядел правильно — расходилась только база.
    Тест перечитывает алерт новым запросом, а не доверяет ответу.
    """
    alert = await make_alert(session, project)
    tokens = await login(client)

    ack_response = await client.post(f"/api/v1/alerts/{alert.id}/ack", headers=auth_headers(tokens))
    assert ack_response.json()["state"] == "acked"

    reread = await client.get(f"/api/v1/alerts/{alert.id}", headers=auth_headers(tokens))
    body = reread.json()

    assert body["state"] == "acked", "ack не сохранён: эскалация бы продолжилась"
    assert body["acked_at"] is not None
    assert body["acked_by"] == "Иван Петров"


async def test_ack_comment_is_stored_in_audit(
    client: AsyncClient, session: AsyncSession, project: Project, admin
) -> None:
    """Комментарий админа попадает в историю, а не теряется по дороге."""
    alert = await make_alert(session, project)
    tokens = await login(client)

    await client.post(
        f"/api/v1/alerts/{alert.id}/ack",
        json={"comment": "почистил логи"},
        headers=auth_headers(tokens),
    )

    detail = await client.get(f"/api/v1/alerts/{alert.id}", headers=auth_headers(tokens))
    history = detail.json()["escalation_history"]

    assert any("почистил логи" in (step["note"] or "") for step in history), history


async def test_ack_of_resolved_alert_is_rejected(
    client: AsyncClient, session: AsyncSession, project: Project, admin
) -> None:
    from app.services.alerts import resolve_alert

    alert = await make_alert(session, project)
    await resolve_alert(session, alert)
    tokens = await login(client)

    response = await client.post(f"/api/v1/alerts/{alert.id}/ack", headers=auth_headers(tokens))
    assert response.status_code == 409


async def test_snooze_sets_limit(
    client: AsyncClient, session: AsyncSession, project: Project, admin
) -> None:
    """Откладывание ограничено — иначе дежурный откладывает всё подряд."""
    alert = await make_alert(session, project)
    tokens = await login(client)

    ok = await client.post(
        f"/api/v1/alerts/{alert.id}/snooze", json={"minutes": 15}, headers=auth_headers(tokens)
    )
    assert ok.status_code == 200
    assert alert.snoozed_until is not None

    too_long = await client.post(
        f"/api/v1/alerts/{alert.id}/snooze", json={"minutes": 600}, headers=auth_headers(tokens)
    )
    assert too_long.status_code in (409, 422)


async def test_alert_detail_includes_escalation_history(
    client: AsyncClient, session: AsyncSession, project: Project, admin
) -> None:
    from app.models import AlertStep

    alert = await make_alert(session, project)
    session.add(
        AlertStep(
            id="stp_1",
            alert_id=alert.id,
            step=1,
            channel="push",
            target_label="Дежурный",
            outcome="delivered",
        )
    )
    await session.flush()

    tokens = await login(client)
    response = await client.get(f"/api/v1/alerts/{alert.id}", headers=auth_headers(tokens))

    assert response.status_code == 200
    history = response.json()["escalation_history"]
    assert len(history) == 1
    assert history[0]["channel"] == "push"
    assert history[0]["target"] == "Дежурный"


async def test_resolve_stops_timers(
    client: AsyncClient, session: AsyncSession, project: Project, admin
) -> None:
    alert = await make_alert(session, project)
    tokens = await login(client)

    response = await client.post(f"/api/v1/alerts/{alert.id}/resolve", headers=auth_headers(tokens))
    assert response.status_code == 200
    assert response.json()["state"] == "resolved"

    reread = await client.get(f"/api/v1/alerts/{alert.id}", headers=auth_headers(tokens))
    assert reread.json()["state"] == "resolved"


async def test_resolved_event_closes_alert_over_http(
    client: AsyncClient, session: AsyncSession, project: Project, admin
) -> None:
    """Источник прислал resolved: таймеры остановлены, дальше тишина."""
    payload = {
        "project": project.name,
        "host": "host-01",
        "check": "disk_space",
        "severity": "critical",
        "summary": "Диск заполнен",
    }
    await client.post("/api/v1/events", json=payload, headers={"X-API-Key": "key-api"})

    resolved = await client.post(
        "/api/v1/events",
        json={**payload, "status": "resolved"},
        headers={"X-API-Key": "key-api"},
    )
    assert resolved.status_code == 202
    assert resolved.json()["duplicate"] is True

    tokens = await login(client)
    listed = await client.get("/api/v1/alerts?only_active=true", headers=auth_headers(tokens))
    assert listed.json()["total"] == 0


async def test_resolved_without_history_returns_404(
    client: AsyncClient, session: AsyncSession, project: Project
) -> None:
    payload = {
        "project": project.name,
        "host": "ghost",
        "check": "disk_space",
        "severity": "critical",
        "summary": "Диск заполнен",
        "status": "resolved",
    }
    response = await client.post("/api/v1/events", json=payload, headers={"X-API-Key": "key-api"})
    assert response.status_code == 404


async def test_reassign_reopens_acked_alert_over_http(
    client: AsyncClient, session: AsyncSession, project: Project, admin
) -> None:
    from app.services.auth import register_admin as _register

    alert = await make_alert(session, project)
    tokens = await login(client)
    headers = auth_headers(tokens)

    await client.post(f"/api/v1/alerts/{alert.id}/ack", headers=headers)
    second = await _register(session, "second@utax.top", "Второй", "verysecret123")

    response = await client.post(
        f"/api/v1/alerts/{alert.id}/reassign",
        json={"admin_id": second.id},
        headers=headers,
    )

    assert response.status_code == 200
    body = response.json()
    assert body["reopened"] is True
    assert body["state"] == "notifying"

    detail = await client.get(f"/api/v1/alerts/{alert.id}", headers=headers)
    history = detail.json()["escalation_history"]
    assert any(step["channel"] == "reassign" for step in history)


async def test_reassign_unknown_admin_returns_404(
    client: AsyncClient, session: AsyncSession, project: Project, admin
) -> None:
    alert = await make_alert(session, project)
    tokens = await login(client)

    response = await client.post(
        f"/api/v1/alerts/{alert.id}/reassign",
        json={"admin_id": "adm_missing"},
        headers=auth_headers(tokens),
    )
    assert response.status_code == 404


async def test_unknown_alert_returns_404(
    client: AsyncClient, session: AsyncSession, project: Project, admin
) -> None:
    tokens = await login(client)
    response = await client.get("/api/v1/alerts/alt_missing", headers=auth_headers(tokens))
    assert response.status_code == 404


# ───────────────────────────── Устройства ─────────────────────────────


async def test_device_registration_is_idempotent(
    client: AsyncClient, session: AsyncSession, project: Project, admin
) -> None:
    """onTokenRefresh прилетает на каждый запуск — дубли устройств быть не должно."""
    tokens = await login(client)
    headers = auth_headers(tokens)
    payload = {"token": "fcm-token-abcdefgh", "platform": "android", "platform_label": "Pixel 8"}

    first = await client.post("/api/v1/devices", json=payload, headers=headers)
    second = await client.post("/api/v1/devices", json=payload, headers=headers)

    assert first.status_code == 201
    assert second.status_code == 201
    assert first.json()["id"] == second.json()["id"]

    listed = await client.get("/api/v1/devices", headers=headers)
    assert len(listed.json()) == 1


async def test_delivery_receipt_updates_device(
    client: AsyncClient, session: AsyncSession, project: Project, admin
) -> None:
    tokens = await login(client)
    headers = auth_headers(tokens)

    await client.post(
        "/api/v1/devices",
        json={"token": "fcm-token-receipt", "platform": "android"},
        headers=headers,
    )

    response = await client.post(
        "/api/v1/devices/delivery",
        json={"token": "fcm-token-receipt"},
        headers=headers,
    )
    assert response.json()["status"] == "recorded"


async def test_delivery_receipt_for_unknown_token_is_not_an_error(
    client: AsyncClient, session: AsyncSession, project: Project, admin
) -> None:
    """Токен мог отозваться после resolve — это штатная ситуация, не ошибка."""
    tokens = await login(client)

    response = await client.post(
        "/api/v1/devices/delivery",
        json={"token": "давно-несуществующий"},
        headers=auth_headers(tokens),
    )
    assert response.status_code == 200
    assert response.json()["status"] == "ignored"


async def test_heartbeat_touches_devices(
    client: AsyncClient, session: AsyncSession, project: Project, admin
) -> None:
    tokens = await login(client)
    headers = auth_headers(tokens)

    await client.post(
        "/api/v1/devices",
        json={"token": "fcm-token-heartbeat", "platform": "android"},
        headers=headers,
    )
    response = await client.post("/api/v1/devices/heartbeat", headers=headers)
    assert response.json()["status"] == "ok"
