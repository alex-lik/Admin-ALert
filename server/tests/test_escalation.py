"""Тесты движка эскалации: воркер, resolved, лимиты, reassign (2.6–2.9, 2.14).

Воркер тестируется на SQLite без блокировок (`use_lock=False`): блокировок
строк там нет, в проде на Postgres включается `FOR UPDATE SKIP LOCKED`.
Логика шагов, таймеров и лимитов от диалекта не зависит.
"""

from collections.abc import AsyncIterator
from datetime import timedelta

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.models import (
    Alert,
    AlertState,
    Base,
    Device,
    EscalationProfile,
    EscalationStepDefinition,
    Project,
)
from app.schemas.events import EventStatus, IncomingEvent, Severity
from app.services import escalation
from app.services.alerts import AlertError, ingest_event, now, reassign_alert, snooze_alert
from app.services.auth import register_admin
from app.services.quiet import QuietWindow, zone_of


@pytest_asyncio.fixture
async def session() -> AsyncIterator[AsyncSession]:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    factory = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    async with factory() as db:
        yield db

    await engine.dispose()


async def make_profile(
    session: AsyncSession,
    profile_id: str = "fast",
    steps: list[tuple[str, str, int]] | None = None,
    max_steps: int = 8,
) -> None:
    """Профиль с нулевыми задержками: шаги просрочены сразу, ждать не нужно."""
    session.add(
        EscalationProfile(
            id=profile_id, description=profile_id, respects_quiet_hours=False, max_steps=max_steps
        )
    )
    await session.flush()
    for number, (channel, target, delay) in enumerate(
        steps if steps is not None else [("push", "current", 0), ("call", "current", 0)],
        start=1,
    ):
        session.add(
            EscalationStepDefinition(
                id=f"esd_{profile_id}_{number}",
                profile_id=profile_id,
                step=number,
                channel=channel,
                delay_seconds=delay,
                target=target,
            )
        )
    await session.flush()


@pytest_asyncio.fixture
async def project(session: AsyncSession) -> Project:
    await make_profile(session)
    proj = Project(id="prj_w", name="w", is_vip=False, api_key="k", default_profile="fast")
    session.add(proj)
    await session.flush()
    return proj


@pytest_asyncio.fixture
async def admin(session: AsyncSession):
    return await register_admin(session, "duty@utax.top", "Дежурный", "verysecret123")


def firing(**overrides: object) -> IncomingEvent:
    payload: dict[str, object] = {
        "project": "w",
        "host": "h1",
        "check": "disk",
        "severity": Severity.CRITICAL,
        "summary": "Диск заполнен",
        "status": EventStatus.FIRING,
    }
    payload.update(overrides)
    return IncomingEvent(**payload)  # type: ignore[arg-type]


async def make_due_alert(session: AsyncSession, project: Project) -> Alert:
    """Алерт, чей таймер уже просрочен: воркер должен взять его сразу."""
    alert, created = await ingest_event(session, project, firing())
    assert created is True
    assert alert.next_action_at is not None, "первый таймер не взведён при приёме"
    return alert


# ───────────────────────────── Воркер ─────────────────────────────


async def test_first_timer_armed_on_ingest(session: AsyncSession, project: Project) -> None:
    """Без взведённого таймера воркер новый алерт не увидел бы никогда."""
    alert, _ = await ingest_event(session, project, firing())
    assert alert.next_action_at is not None
    assert alert.next_action_at <= now()


async def test_worker_full_lifecycle_to_expiry(
    session: AsyncSession, project: Project, admin
) -> None:
    """Полный цикл: шаг 1 → шаг 2 → истечение. Три прохода, без ожидания."""
    from app.models import Device

    # Устройство на связи: иначе push честно сфейлится (см. отдельный тест),
    # а здесь проверяется нормальный путь.
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
    await session.flush()

    alert = await make_due_alert(session, project)

    assert await escalation.poll_once(session, use_lock=False) == 1
    assert alert.current_step == 1
    assert alert.state == AlertState.NOTIFYING

    # Второй проход сразу же: следующий шаг тоже просрочен (задержка 0).
    assert await escalation.poll_once(session, use_lock=False) == 1
    assert alert.current_step == 2

    # Шаги кончились: следующий проход после паузы признаёт алерт истёкшим.
    alert.next_action_at = now() - timedelta(seconds=1)
    await session.flush()
    assert await escalation.poll_once(session, use_lock=False) == 1
    assert alert.state == AlertState.EXPIRED
    assert alert.next_action_at is None


async def test_worker_does_not_double_process(
    session: AsyncSession, project: Project, admin
) -> None:
    """Повторный проход не выполняет шаг дважды и не плодит записи аудита."""
    from sqlalchemy import func, select

    from app.models import AlertStep

    alert = await make_due_alert(session, project)
    await escalation.poll_once(session, use_lock=False)

    # Уводим таймер в будущее вручную: следующий шаг ещё не наступил.
    alert.next_action_at = now() + timedelta(hours=1)
    await session.flush()

    assert await escalation.poll_once(session, use_lock=False) == 0

    count = (
        await session.execute(
            select(func.count()).select_from(AlertStep).where(AlertStep.alert_id == alert.id)
        )
    ).scalar_one()
    assert count == 1


async def test_worker_skips_acked_alert(session: AsyncSession, project: Project, admin) -> None:
    """Ack между выборкой и обработкой: шаг по закрытому алерту не выполняется."""
    from app.services.alerts import ack_alert

    alert = await make_due_alert(session, project)
    await ack_alert(session, alert, admin.id, admin.name)

    assert await escalation.poll_once(session, use_lock=False) == 0
    assert alert.current_step == 0


async def test_stale_device_fails_push_and_accelerates(
    session: AsyncSession, project: Project, admin
) -> None:
    """Молчащий телефон push не ждёт: шаг сорван, следующий — раньше расписания."""
    session.add(
        Device(
            id="dev_old",
            admin_id=admin.id,
            token="tok-old",
            platform="android",
            last_seen_at=now() - timedelta(hours=2),
            is_active=True,
        )
    )
    await session.flush()

    alert = await make_due_alert(session, project)
    assert await escalation.poll_once(session, use_lock=False) == 1

    from sqlalchemy import select

    from app.models import AlertStep

    steps = (
        (await session.execute(select(AlertStep).where(AlertStep.alert_id == alert.id)))
        .scalars()
        .all()
    )
    assert len(steps) == 1
    assert steps[0].outcome == "failed"
    assert "не на связи" in (steps[0].note or "")
    # Следующий шаг назначен раньше, чем по расписанию профиля.
    assert alert.next_action_at is not None
    assert alert.next_action_at <= now() + timedelta(minutes=1)


async def test_fresh_device_step_is_recorded(
    session: AsyncSession, project: Project, admin
) -> None:
    """Устройство на связи: шаг уходит в диспетчер и честно помечается."""
    session.add(
        Device(
            id="dev_new",
            admin_id=admin.id,
            token="tok-new",
            platform="android",
            last_seen_at=now(),
            is_active=True,
        )
    )
    await session.flush()

    alert = await make_due_alert(session, project)
    await escalation.poll_once(session, use_lock=False)

    from sqlalchemy import select

    from app.models import AlertStep

    steps = (
        (await session.execute(select(AlertStep).where(AlertStep.alert_id == alert.id)))
        .scalars()
        .all()
    )
    assert len(steps) == 1
    # NullDispatcher не врёт про доставку: отправка не подключена.
    assert steps[0].outcome == "skipped"


async def test_target_falls_back_without_schedule(
    session: AsyncSession, project: Project, admin
) -> None:
    """Без расписания шаг адресуется первому активному админу.

    Метка говорит, что расписание не заведено (задача 2.10), а не просто
    «не расписано»: дежурный должен понимать, что чинить.
    """
    target = await escalation.resolve_target(session, "current", now())
    assert target.admin_id == admin.id
    assert "расписание не заведено" in target.label


async def test_target_all_without_admins(session: AsyncSession, project: Project) -> None:
    """Некому пейджить: метка говорит об этом прямо, а не молчит."""
    target = await escalation.resolve_target(session, "all", now())
    assert target.admin_id is None


# ───────────────────────────── Тихие часы (2.13) ─────────────────────────────

# Окно, в которое попадает «сейчас»: 00:00–23:59 по часовому поясу админа.
ALWAYS_QUIET = QuietWindow(start_hour=0, end_hour=23)
NEVER_QUIET = QuietWindow(start_hour=5, end_hour=5)


def _make_due(alert: Alert) -> None:
    """Сделать таймер просроченным: иначе процесс вернёт skipped, а не проверит шаг."""
    alert.next_action_at = now() - timedelta(seconds=1)


async def _profile(session: AsyncSession, profile_id: str, *, respects: bool) -> None:
    """Профиль с одним шагом и заданным отношением к тихим часам."""
    session.add(
        EscalationProfile(
            id=profile_id,
            description=profile_id,
            respects_quiet_hours=respects,
            max_steps=3,
        )
    )
    session.add(
        EscalationStepDefinition(
            id=f"esd_{profile_id}",
            profile_id=profile_id,
            step=1,
            channel="push",
            delay_seconds=60,
            target="current",
        )
    )
    await session.flush()


async def test_warning_waits_until_morning(session: AsyncSession, admin) -> None:
    """Warning ночью не будит: шаг переносится на конец тихих часов."""
    await _profile(session, "warning", respects=True)
    session.add(Project(id="prj_w", name="w", is_vip=False, api_key="k", default_profile="warning"))
    await session.flush()

    project = await session.get(Project, "prj_w")
    event = IncomingEvent(
        project="w",
        host="h1",
        check="disk",
        severity=Severity.WARNING,
        summary="Место заканчивается",
        status=EventStatus.FIRING,
    )
    alert, _ = await ingest_event(session, project, event)
    await session.flush()
    _make_due(alert)

    result = await escalation.process_due_alert(
        session, alert.id, escalation.NullDispatcher(), window=ALWAYS_QUIET
    )

    assert result == "deferred"
    assert alert.state == AlertState.TRIGGERED, "алерт ждёт, а не эскалирует"
    assert alert.current_step == 0, "шаг не выполнен"
    assert alert.next_action_at is not None
    # Таймер уехал в будущее — до утра.
    assert alert.next_action_at > now()


async def test_critical_ignores_quiet_hours(session: AsyncSession, admin) -> None:
    """Critical ночью эскалирует как обычно: сертификат не подождёт до утра."""
    await _profile(session, "critical", respects=False)
    session.add(
        Project(id="prj_c", name="c", is_vip=False, api_key="kc", default_profile="critical")
    )
    await session.flush()

    project = await session.get(Project, "prj_c")
    event = IncomingEvent(
        project="c",
        host="h1",
        check="ssl",
        severity=Severity.CRITICAL,
        summary="Сертификат истёк",
        status=EventStatus.FIRING,
    )
    alert, _ = await ingest_event(session, project, event)
    await session.flush()
    _make_due(alert)

    result = await escalation.process_due_alert(
        session, alert.id, escalation.NullDispatcher(), window=ALWAYS_QUIET
    )

    assert result == "step:1"
    assert alert.current_step == 1
    assert alert.state == AlertState.NOTIFYING


async def test_second_step_also_deferred(session: AsyncSession, admin) -> None:
    """Тихие часы действуют на каждый шаг, а не только на первый.

    Иначе цепочка push → telegram → call ночью разбудила бы на push, а к утру
    добралась бы до звонка: тихие часы получились бы только для первого шага.
    """
    await _profile(session, "warning", respects=True)
    session.add(Project(id="prj_w", name="w", is_vip=False, api_key="k", default_profile="warning"))
    session.add(
        Device(
            id="dev_ok",
            admin_id=admin.id,
            token="tok",
            platform="android",
            last_seen_at=now(),
            is_active=True,
        )
    )
    await session.flush()

    project = await session.get(Project, "prj_w")
    alert, _ = await ingest_event(
        session,
        project,
        IncomingEvent(
            project="w",
            host="h1",
            check="disk",
            severity=Severity.WARNING,
            summary="Место заканчивается",
            status=EventStatus.FIRING,
        ),
    )
    await session.flush()
    _make_due(alert)

    dispatcher = escalation.NullDispatcher()
    first = await escalation.process_due_alert(session, alert.id, dispatcher, window=ALWAYS_QUIET)
    assert first == "deferred"

    # Отодвигаем таймер в прошлое, чтобы проверить второй шаг того же профиля.
    alert.next_action_at = now() - timedelta(seconds=1)
    await session.flush()

    second = await escalation.process_due_alert(session, alert.id, dispatcher, window=ALWAYS_QUIET)
    assert second == "deferred"
    assert alert.next_action_at > now(), "второй шаг тоже ждёт утра"


async def test_outside_quiet_hours_alert_escalates(session: AsyncSession, admin) -> None:
    """Днём тихие часы не действуют — alert идёт по расписанию."""
    await _profile(session, "warning", respects=True)
    session.add(Project(id="prj_w", name="w", is_vip=False, api_key="k", default_profile="warning"))
    await session.flush()

    project = await session.get(Project, "prj_w")
    alert, _ = await ingest_event(
        session,
        project,
        IncomingEvent(
            project="w",
            host="h1",
            check="disk",
            severity=Severity.WARNING,
            summary="Место заканчивается",
            status=EventStatus.FIRING,
        ),
    )
    await session.flush()
    _make_due(alert)

    result = await escalation.process_due_alert(
        session, alert.id, escalation.NullDispatcher(), window=NEVER_QUIET
    )

    assert result == "step:1"


async def test_poll_counts_deferred_as_not_processed(session: AsyncSession, admin) -> None:
    """Отложенный алерт — это не работа: показывать его обработанным врёт."""
    await _profile(session, "warning", respects=True)
    session.add(Project(id="prj_w", name="w", is_vip=False, api_key="k", default_profile="warning"))
    await session.flush()

    project = await session.get(Project, "prj_w")
    await ingest_event(
        session,
        project,
        IncomingEvent(
            project="w",
            host="h1",
            check="disk",
            severity=Severity.WARNING,
            summary="Место заканчивается",
            status=EventStatus.FIRING,
        ),
    )
    await session.commit()

    processed = await escalation.poll_once(session, use_lock=False, window=ALWAYS_QUIET)

    assert processed == 0
    assert await escalation.count_due(session) == 0, "алерт перенесён, а не забыт"


async def test_respects_quiet_hours_uses_recipient_timezone(session: AsyncSession, admin) -> None:
    """Тихие часы считаются по времени адресата, а не сервера.

    Трюк: выбираем окно, которое тихо по UTC в момент, когда по Киеву уже
    утро. Если бы считалось по серверному времени, тест прошёл бы наоборот.
    """
    admin.timezone = "Europe/Kyiv"
    await session.flush()

    await _profile(session, "warning", respects=True)
    session.add(Project(id="prj_w", name="w", is_vip=False, api_key="k", default_profile="warning"))
    await session.flush()

    project = await session.get(Project, "prj_w")
    alert, _ = await ingest_event(
        session,
        project,
        IncomingEvent(
            project="w",
            host="h1",
            check="disk",
            severity=Severity.WARNING,
            summary="Место заканчивается",
            status=EventStatus.FIRING,
        ),
    )
    await session.flush()
    _make_due(alert)

    # Окно 00:00–23:59 по UTC: сейчас тихо по серверу. По Киеву сейчас UTC+3,
    # значит локальный час >= 23 тихо, а если локально утро (час < 23) — нет.
    window = QuietWindow(start_hour=23, end_hour=22)  # тихо только 23:00–21:59 UTC
    local_hour = now().astimezone(zone_of("Europe/Kyiv")).hour

    result = await escalation.process_due_alert(
        session, alert.id, escalation.NullDispatcher(), window=window
    )

    if local_hour >= 23 or local_hour < 22:
        assert result == "deferred", f"локально {local_hour:02d}: тихо по киевскому времени"
    else:
        assert result == "step:1", f"локально {local_hour:02d}: утро, тихие часы кончились"


# ───────────────────────────── Resolved ─────────────────────────────


async def test_resolved_event_closes_alert(session: AsyncSession, project: Project, admin) -> None:
    """Задача 2.6: resolved останавливает таймеры."""
    alert = await make_due_alert(session, project)
    alert.next_action_at = now() + timedelta(minutes=5)
    alert.current_step = 2
    await session.flush()

    closed, created = await ingest_event(session, project, firing(status=EventStatus.RESOLVED))

    assert created is False
    assert closed.id == alert.id
    assert closed.state == AlertState.RESOLVED
    assert closed.next_action_at is None, "таймер не остановлен"


async def test_resolved_without_history_is_rejected(
    session: AsyncSession, project: Project
) -> None:
    """Resolved без истории: выдумывать алерт не будем, маршрут отдаст 404."""
    with pytest.raises(AlertError):
        await ingest_event(session, project, firing(status=EventStatus.RESOLVED))


async def test_resolved_after_resolved_returns_history(
    session: AsyncSession, project: Project
) -> None:
    """Двойной resolved: второй ничего не меняет, счётчик не растёт."""
    alert = await make_due_alert(session, project)
    await ingest_event(session, project, firing(status=EventStatus.RESOLVED))

    again, created = await ingest_event(session, project, firing(status=EventStatus.RESOLVED))

    assert created is False
    assert again.id == alert.id
    assert again.occurrences == 1


# ───────────────────────────── Snooze и reassign ─────────────────────────────


async def test_snooze_third_time_within_hour_is_rejected(
    session: AsyncSession, project: Project
) -> None:
    """Лимит откладываний по количеству: не больше 2 раз в час на алерт."""
    alert = await make_due_alert(session, project)

    await snooze_alert(session, alert, 5)
    await snooze_alert(session, alert, 5)

    with pytest.raises(AlertError, match="лимит"):
        await snooze_alert(session, alert, 5)


async def test_reassign_reopens_acked_alert(session: AsyncSession, project: Project, admin) -> None:
    """Передача подтверждённого алерта снимает чужое подтверждение."""
    from app.services.alerts import ack_alert

    alert = await make_due_alert(session, project)
    await ack_alert(session, alert, admin.id, admin.name)

    second = await register_admin(session, "second@utax.top", "Второй", "verysecret123")
    reopened = await reassign_alert(session, alert, second.id, second.name, admin.name, "ты ближе")

    assert reopened is True
    assert alert.state == AlertState.NOTIFYING
    assert alert.acked_at is None
    assert alert.acked_by_id is None
    # Таймеры и шаг не тронуты: эскалация продолжается с того же места.
    assert alert.current_step == 0


async def test_reassign_is_audited(session: AsyncSession, project: Project, admin) -> None:
    """Передача пишется в аудит: иначе теряется ответ «кому принадлежит алерт»."""
    from sqlalchemy import select

    from app.models import AlertStep

    alert = await make_due_alert(session, project)
    second = await register_admin(session, "second@utax.top", "Второй", "verysecret123")
    await reassign_alert(session, alert, second.id, second.name, admin.name)

    steps = (
        (
            await session.execute(
                select(AlertStep).where(
                    AlertStep.alert_id == alert.id, AlertStep.channel == "reassign"
                )
            )
        )
        .scalars()
        .all()
    )

    assert len(steps) == 1
    assert steps[0].target_admin_id == second.id


async def test_reassign_resolved_is_rejected(
    session: AsyncSession, project: Project, admin
) -> None:
    from app.services.alerts import resolve_alert

    alert = await make_due_alert(session, project)
    await resolve_alert(session, alert)

    second = await register_admin(session, "second@utax.top", "Второй", "verysecret123")
    with pytest.raises(AlertError):
        await reassign_alert(session, alert, second.id, second.name, admin.name)


# ───────────────────────────── Список ─────────────────────────────


async def test_list_ordering_vip_critical_first(session: AsyncSession) -> None:
    """Порядок как на экране: активные → VIP → критичность → время."""
    from app.services.alerts import list_alerts

    await make_profile(session, "warning", steps=[("push", "current", 3600)])
    await make_profile(session, "critical-vip", steps=[("push", "current", 0)])
    vip_project = Project(id="prj_vip", name="vip", is_vip=True, api_key="v")
    plain_project = Project(id="prj_plain", name="plain", is_vip=False, api_key="p")
    session.add_all([vip_project, plain_project])
    await session.flush()

    # Обычный warning раньше по времени — но должен оказаться ниже.
    await ingest_event(
        session,
        plain_project,
        firing(project="plain", host="h", check="c", severity=Severity.WARNING),
    )
    vip_alert, _ = await ingest_event(
        session,
        vip_project,
        firing(project="vip", host="h", check="c", severity=Severity.CRITICAL),
    )

    items, total, active, critical = await list_alerts(session)

    assert total == 2
    assert items[0].id == vip_alert.id
