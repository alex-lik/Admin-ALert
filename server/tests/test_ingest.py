"""Тесты приёма событий и дедупликации (задачи 2.3, 2.4, 5.1).

Работают на SQLite в памяти — тот же код, что в проде на Postgres.
"""

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.models import AlertState, Base, EscalationProfile, Project
from app.schemas.events import EventStatus, IncomingEvent, Severity
from app.services.alerts import ingest_event


@pytest_asyncio.fixture
async def session() -> AsyncSession:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    factory = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    async with factory() as db:
        yield db

    await engine.dispose()


@pytest_asyncio.fixture
async def project(session: AsyncSession) -> Project:
    for profile_id, respects in (("warning", True), ("critical", False), ("critical-vip", False)):
        session.add(
            EscalationProfile(id=profile_id, description=profile_id, respects_quiet_hours=respects)
        )

    proj = Project(id="prj_test", name="test", is_vip=False, api_key="key-test")
    session.add(proj)
    await session.flush()
    return proj


def make_event(**overrides: object) -> IncomingEvent:
    payload: dict[str, object] = {
        "project": "test",
        "host": "host-01",
        "check": "disk_space",
        "severity": Severity.WARNING,
        "summary": "Диск заполнен",
        "status": EventStatus.FIRING,
    }
    payload.update(overrides)
    return IncomingEvent(**payload)  # type: ignore[arg-type]


async def test_first_event_creates_alert(session: AsyncSession, project: Project) -> None:
    alert, created = await ingest_event(session, project, make_event())

    assert created is True
    assert alert.occurrences == 1
    assert alert.state == AlertState.TRIGGERED
    # dedup_key собирается из project:check:host, если источник его не прислал.
    assert alert.dedup_key == "test:disk_space:host-01"


async def test_repeat_event_increments_counter_without_restarting_escalation(
    session: AsyncSession, project: Project
) -> None:
    """Задача 2.4: то самое поведение, ради которого нужна дедупликация."""
    first, created_first = await ingest_event(session, project, make_event())
    first.next_action_at = first.first_seen_at
    first.current_step = 3
    await session.flush()

    second, created_second = await ingest_event(session, project, make_event())

    assert created_first is True
    assert created_second is False
    assert second.id == first.id
    assert second.occurrences == 2
    # Ключевое: шаг эскалации и таймер не сдвинулись. Иначе шторм событий
    # бесконечно откладывал бы звонок, и алерт не дозвонится никогда.
    assert second.current_step == 3
    assert second.next_action_at == first.next_action_at


async def test_explicit_dedup_key_is_respected(session: AsyncSession, project: Project) -> None:
    """Явный dedup_key от источника важнее собранного из project:check:host."""
    first, created_first = await ingest_event(session, project, make_event(dedup_key="мой-ключ"))
    assert created_first is True
    assert first.dedup_key == "мой-ключ"

    # Повтор с тем же явным ключом — тот же инцидент.
    second, created_second = await ingest_event(session, project, make_event(dedup_key="мой-ключ"))
    assert created_second is False
    assert second.id == first.id
    assert second.occurrences == 2

    # А вот другой ключ — другой инцидент, даже при тех же host и check.
    third, created_third = await ingest_event(session, project, make_event(dedup_key="другой-ключ"))
    assert created_third is True
    assert third.id != first.id


async def test_different_host_creates_separate_alert(
    session: AsyncSession, project: Project
) -> None:
    first, created_first = await ingest_event(session, project, make_event())
    second, created_second = await ingest_event(session, project, make_event(host="host-02"))

    assert created_first is True
    assert created_second is True
    assert first.id != second.id


async def test_vip_project_gets_aggressive_profile(session: AsyncSession) -> None:
    """Задача 2.12: VIP стартует с более агрессивного профиля."""
    session.add(
        EscalationProfile(id="warning", description="w", respects_quiet_hours=True),
    )
    session.add(
        EscalationProfile(id="critical-vip", description="vip", respects_quiet_hours=False),
    )
    vip = Project(id="prj_vip", name="vip", is_vip=True, api_key="key-vip")
    session.add(vip)
    await session.flush()

    alert, _ = await ingest_event(session, vip, make_event(severity=Severity.WARNING))

    # Даже warning у VIP-проекта уходит по профилю critical-vip.
    assert alert.profile_id == "critical-vip"


async def test_critical_severity_picks_critical_profile(
    session: AsyncSession, project: Project
) -> None:
    alert, _ = await ingest_event(session, project, make_event(severity=Severity.CRITICAL))
    assert alert.profile_id == "critical"


async def test_blank_summary_is_rejected() -> None:
    """Пустая сводка не дойдёт до схемы — раньше об этом заботился валидатор."""
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        make_event(summary="   ")


async def test_host_and_check_are_created_on_the_fly(
    session: AsyncSession, project: Project
) -> None:
    """Источник не обязан регистрировать хосты заранее."""
    await ingest_event(session, project, make_event(host="brand-new-host", check="new_check"))

    from sqlalchemy import select

    from app.models import Check, Host

    hosts = (await session.execute(select(Host))).scalars().all()
    checks = (await session.execute(select(Check))).scalars().all()

    assert [h.name for h in hosts] == ["brand-new-host"]
    assert [c.key for c in checks] == ["new_check"]
