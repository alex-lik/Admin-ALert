"""Тесты расписания дежурства (задача 2.10).

Расписание — то место, где ошибка не роняет систему, а молча будит не того
человека. Поэтому проверяется не «работает ли», а «не может ли получиться
неправильное расписание»:

- две primary-смены одновременно (тогда выбор неоднозначен);
- смена в прошлом (она не может закрыть уже начавшееся дежурство);
- дыра в расписании (она обязана быть видна, а не тихо закрываться
  фолбэком);
- ротация из одного человека (не должна молча множить смены на месяц).

Плюс проверяется продолжение круга: ротация материализована, а не является
правилом, поэтому её нужно досоздавать — и она должна продолжиться с того
же места, а не начаться заново.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import timedelta

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.models import Base, OnCallShift
from app.services import escalation, oncall
from app.services.alerts import now
from app.services.auth import register_admin
from app.services.oncall import ScheduleError


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
    """Три админа для ротации."""
    return [
        await register_admin(session, f"d{i}@utax.top", f"Дежурный {i}", "verysecret123")
        for i in range(3)
    ]


# ───────────────────────────── Создание смены ─────────────────────────────


async def test_shift_is_created(session: AsyncSession, crew: list) -> None:
    start = now() + timedelta(hours=1)

    shift = await oncall.create_shift(
        session,
        admin_id=crew[0].id,
        role="primary",
        starts_at=start,
        ends_at=start + timedelta(hours=12),
    )

    assert shift.id.startswith("shf_")
    assert shift.role == "primary"


async def test_overlapping_shifts_rejected(session: AsyncSession, crew: list) -> None:
    """Две primary-смены одновременно: система выбрала бы одну наугад."""
    start = now() + timedelta(hours=1)
    await oncall.create_shift(
        session,
        admin_id=crew[0].id,
        role="primary",
        starts_at=start,
        ends_at=start + timedelta(hours=12),
    )

    with pytest.raises(ScheduleError, match="пересекается"):
        await oncall.create_shift(
            session,
            admin_id=crew[1].id,
            role="primary",
            starts_at=start + timedelta(hours=6),
            ends_at=start + timedelta(hours=18),
        )


async def test_adjacent_shifts_allowed(session: AsyncSession, crew: list) -> None:
    """Смена, начинающаяся ровно в момент конца другой, не пересекается с ней."""
    start = now() + timedelta(hours=1)
    await oncall.create_shift(
        session,
        admin_id=crew[0].id,
        role="primary",
        starts_at=start,
        ends_at=start + timedelta(hours=12),
    )

    second = await oncall.create_shift(
        session,
        admin_id=crew[1].id,
        role="primary",
        starts_at=start + timedelta(hours=12),
        ends_at=start + timedelta(hours=24),
    )

    assert second.admin_id == crew[1].id


async def test_roles_do_not_clash(session: AsyncSession, crew: list) -> None:
    """primary и backup в одном окне — нормально: запасной включается, когда
    основной не ответил."""
    start = now() + timedelta(hours=1)

    await oncall.create_shift(
        session,
        admin_id=crew[0].id,
        role="primary",
        starts_at=start,
        ends_at=start + timedelta(hours=12),
    )
    backup = await oncall.create_shift(
        session,
        admin_id=crew[1].id,
        role="backup",
        starts_at=start,
        ends_at=start + timedelta(hours=12),
    )

    assert backup.role == "backup"


async def test_shift_in_past_rejected(session: AsyncSession, crew: list) -> None:
    """Смена, начавшаяся вчера, не может закрыть уже начавшееся дежурство."""
    with pytest.raises(ScheduleError, match="прошлом"):
        await oncall.create_shift(
            session,
            admin_id=crew[0].id,
            role="primary",
            starts_at=now() - timedelta(hours=2),
            ends_at=now() + timedelta(hours=2),
        )


async def test_inverted_shift_rejected(session: AsyncSession, crew: list) -> None:
    start = now() + timedelta(hours=2)
    with pytest.raises(ScheduleError, match="позже начала"):
        await oncall.create_shift(
            session,
            admin_id=crew[0].id,
            role="primary",
            starts_at=start,
            ends_at=start - timedelta(hours=1),
        )


async def test_unknown_admin_rejected(session: AsyncSession) -> None:
    start = now() + timedelta(hours=1)
    with pytest.raises(ScheduleError, match="не найден"):
        await oncall.create_shift(
            session,
            admin_id="adm_нет",
            role="primary",
            starts_at=start,
            ends_at=start + timedelta(hours=1),
        )


async def test_inactive_admin_cannot_be_on_call(session: AsyncSession, crew: list) -> None:
    """Отключённый админ не может дежурить: система не должна будить того,
    кого вывели из оборота."""
    crew[0].is_active = False
    await session.flush()
    start = now() + timedelta(hours=1)

    with pytest.raises(ScheduleError, match="отключён"):
        await oncall.create_shift(
            session,
            admin_id=crew[0].id,
            role="primary",
            starts_at=start,
            ends_at=start + timedelta(hours=1),
        )


async def test_unknown_role_rejected(session: AsyncSession, crew: list) -> None:
    start = now() + timedelta(hours=1)
    with pytest.raises(ScheduleError, match="роль"):
        await oncall.create_shift(
            session,
            admin_id=crew[0].id,
            role="директор",
            starts_at=start,
            ends_at=start + timedelta(hours=1),
        )


# ───────────────────────────── Ротация ─────────────────────────────


async def test_rotation_follows_given_order(session: AsyncSession, crew: list) -> None:
    """Порядок дежурства задаётся явно, а не «по дате создания админов»."""
    start = now() + timedelta(hours=1)

    await oncall.rotate(
        session,
        admin_ids=[crew[2].id, crew[0].id, crew[1].id],
        role="primary",
        starts_at=start,
        shift_hours=12,
        days=2,
    )

    shifts = (
        (await session.execute(select(OnCallShift).order_by(OnCallShift.starts_at))).scalars().all()
    )

    assert [s.admin_id for s in shifts[:3]] == [crew[2].id, crew[0].id, crew[1].id]


async def test_rotation_covers_whole_period(session: AsyncSession, crew: list) -> None:
    """Смены идут без разрывов: дыра означала бы фолбэк на «первого по списку»."""
    start = now() + timedelta(hours=1)

    await oncall.rotate(
        session,
        admin_ids=[a.id for a in crew],
        role="primary",
        starts_at=start,
        shift_hours=8,
        days=3,
    )

    shifts = (
        (await session.execute(select(OnCallShift).order_by(OnCallShift.starts_at).limit(20)))
        .scalars()
        .all()
    )

    for previous, following in zip(shifts, shifts[1:], strict=False):
        assert following.starts_at == previous.ends_at, "разрыв в расписании"


async def test_rotation_requires_admins(session: AsyncSession) -> None:
    with pytest.raises(ScheduleError, match="хотя бы один"):
        await oncall.rotate(
            session,
            admin_ids=[],
            role="primary",
            starts_at=now(),
            shift_hours=8,
            days=1,
        )


async def test_rotation_rejects_duplicates(session: AsyncSession, crew: list) -> None:
    """Повтор одного админа в круге — смена была бы впустую."""
    with pytest.raises(ScheduleError, match="повторяется"):
        await oncall.rotate(
            session,
            admin_ids=[crew[0].id, crew[0].id],
            role="primary",
            starts_at=now(),
            shift_hours=8,
            days=1,
        )


async def test_rotation_rejects_zero_length(session: AsyncSession, crew: list) -> None:
    with pytest.raises(ScheduleError, match="длительность"):
        await oncall.rotate(
            session,
            admin_ids=[crew[0].id],
            role="primary",
            starts_at=now(),
            shift_hours=0,
            days=1,
        )


# ───────────────────────────── Горизонт ─────────────────────────────


async def test_horizon_extends_rotation(session: AsyncSession, crew: list) -> None:
    """Материализованную ротацию нужно продолжать — иначе она кончится."""
    start = now() + timedelta(hours=1)
    await oncall.rotate(
        session,
        admin_ids=[a.id for a in crew],
        role="primary",
        starts_at=start,
        shift_hours=12,
        days=2,
    )
    before = len(list((await session.execute(select(OnCallShift))).scalars()))

    created = await oncall.ensure_horizon(session, horizon_days=10)

    assert created > 0
    after = len(list((await session.execute(select(OnCallShift))).scalars()))
    assert after == before + created


async def test_horizon_is_noop_when_coverage_sufficient(session: AsyncSession, crew: list) -> None:
    """При нормальном расписании горизонт не создаёт ничего: иначе воркер
    писал бы в БД на каждом проходе."""
    start = now() + timedelta(hours=1)
    await oncall.rotate(
        session,
        admin_ids=[a.id for a in crew],
        role="primary",
        starts_at=start,
        shift_hours=12,
        days=60,
    )

    assert await oncall.ensure_horizon(session, horizon_days=30) == 0


async def test_horizon_continues_the_same_cycle(session: AsyncSession, crew: list) -> None:
    """Продолжение круга начинается со следующего админа, а не с начала."""
    start = now() + timedelta(hours=1)
    await oncall.rotate(
        session,
        admin_ids=[a.id for a in crew],
        role="primary",
        starts_at=start,
        shift_hours=12,
        days=2,
    )
    await oncall.ensure_horizon(session, horizon_days=10)

    shifts = (
        (
            await session.execute(
                select(OnCallShift)
                # >=, а не >: первая продолженная смена начинается ровно там,
                # где закончилась последняя исходная, и её легко потерять.
                .where(OnCallShift.starts_at >= start + timedelta(days=2))
                .order_by(OnCallShift.starts_at)
            )
        )
        .scalars()
        .all()
    )

    assert shifts, "продолжение должно создать смены"
    # За 2 дня по 12 часов выходит 4 смены: d0, d1, d2, d0. Значит после них
    # круг идёт дальше — к d1. Если бы продолжение начиналось с начала круга,
    # следующей была бы d0, и это означало бы, что одна смена «потерялась».
    assert shifts[0].admin_id == crew[1].id


async def test_single_admin_rotation_is_not_invented(session: AsyncSession, crew: list) -> None:
    """Ротация из одного человека не размножается на месяц: это замаскирует
    отсутствие расписания."""
    start = now() + timedelta(hours=1)
    await oncall.rotate(
        session,
        admin_ids=[crew[0].id],
        role="primary",
        starts_at=start,
        shift_hours=24,
        days=2,
    )
    before = len(list((await session.execute(select(OnCallShift))).scalars()))

    assert await oncall.ensure_horizon(session, horizon_days=10) == 0
    after = len(list((await session.execute(select(OnCallShift))).scalars()))
    assert after == before


# ───────────────────────────── Кто дежурит ─────────────────────────────


async def test_current_roster(session: AsyncSession, crew: list) -> None:
    start = now() - timedelta(hours=1)
    await oncall.create_shift(
        session,
        admin_id=crew[1].id,
        role="primary",
        starts_at=start,
        ends_at=start + timedelta(hours=6),
        allow_past=True,
    )

    current = await oncall.current_roster(session, role="primary")

    assert current is not None
    assert current.admin_id == crew[1].id
    assert current.admin_name == crew[1].name


async def test_roster_is_none_outside_shift(session: AsyncSession, crew: list) -> None:
    start = now() + timedelta(hours=5)
    await oncall.create_shift(
        session,
        admin_id=crew[0].id,
        role="primary",
        starts_at=start,
        ends_at=start + timedelta(hours=6),
    )

    assert await oncall.current_roster(session, role="primary") is None


async def test_roster_none_when_admin_disabled(session: AsyncSession, crew: list) -> None:
    """Смена есть, а дежурить некому: возвращаем None, а не отключённого."""
    start = now() - timedelta(hours=1)
    await oncall.create_shift(
        session,
        admin_id=crew[0].id,
        role="primary",
        starts_at=start,
        ends_at=start + timedelta(hours=6),
        allow_past=True,
    )
    crew[0].is_active = False
    await session.flush()

    assert await oncall.current_roster(session, role="primary") is None


# ───────────────────────────── Покрытие ─────────────────────────────


async def test_coverage_reports_gap(session: AsyncSession, crew: list) -> None:
    """Молчаливая дырка — худший вид поломки: система работает, но дежурит не
    тот. Она обязана быть видна."""
    now_moment = now()
    await oncall.create_shift(
        session,
        admin_id=crew[0].id,
        role="primary",
        starts_at=now_moment + timedelta(hours=1),
        ends_at=now_moment + timedelta(hours=3),
        allow_past=True,
    )

    gaps = await oncall.coverage(
        session, role="primary", from_at=now_moment, to_at=now_moment + timedelta(hours=4)
    )

    assert gaps, "дыра до первой смены должна быть найдена"
    assert gaps[0]["reason"] == "нет смены"


async def test_coverage_gap_between_shifts(session: AsyncSession, crew: list) -> None:
    moment = now()
    await oncall.create_shift(
        session,
        admin_id=crew[0].id,
        role="primary",
        starts_at=moment + timedelta(hours=1),
        ends_at=moment + timedelta(hours=2),
        allow_past=True,
    )
    await oncall.create_shift(
        session,
        admin_id=crew[1].id,
        role="primary",
        starts_at=moment + timedelta(hours=5),
        ends_at=moment + timedelta(hours=6),
        allow_past=True,
    )

    gaps = await oncall.coverage(
        session, role="primary", from_at=moment, to_at=moment + timedelta(hours=7)
    )

    assert len(gaps) == 3, "до первой, между сменами и после последней"


async def test_coverage_full_is_no_gaps(session: AsyncSession, crew: list) -> None:
    moment = now()
    await oncall.rotate(
        session,
        admin_ids=[a.id for a in crew],
        role="primary",
        starts_at=moment - timedelta(hours=1),
        shift_hours=4,
        days=2,
        allow_past=True,
    )

    gaps = await oncall.coverage(
        session, role="primary", from_at=moment, to_at=moment + timedelta(hours=10)
    )

    assert gaps == []


# ───────────────────────────── Влияние на эскалацию ─────────────────────────────


async def test_escalation_uses_scheduled_admin(session: AsyncSession, crew: list) -> None:
    """Главная проверка: шаг уходит тому, кто в расписании, а не первому."""
    start = now() - timedelta(hours=1)
    await oncall.create_shift(
        session,
        admin_id=crew[2].id,
        role="primary",
        starts_at=start,
        ends_at=start + timedelta(hours=12),
        allow_past=True,
    )

    target = await escalation.resolve_target(session, "current", now())

    assert target.admin_id == crew[2].id
    assert "Дежурный" in target.label


async def test_fallback_reason_when_no_schedule(session: AsyncSession, crew: list) -> None:
    """Метка фолбэка должна говорить «расписание не заведено», чтобы дежурный
    знал, что чинить."""
    target = await escalation.resolve_target(session, "current", now())

    assert "расписание не заведено" in target.label


async def test_fallback_reason_when_gap(session: AsyncSession, crew: list) -> None:
    """Расписание заведено, но в нём дыра — это другая поломка и другая метка."""
    start = now() + timedelta(hours=5)
    await oncall.create_shift(
        session,
        admin_id=crew[1].id,
        role="primary",
        starts_at=start,
        ends_at=start + timedelta(hours=6),
    )

    target = await escalation.resolve_target(session, "current", now())

    assert "дыра" in target.label


async def test_backup_role_addressed(session: AsyncSession, crew: list) -> None:
    start = now() - timedelta(hours=1)
    await oncall.create_shift(
        session,
        admin_id=crew[0].id,
        role="primary",
        starts_at=start,
        ends_at=start + timedelta(hours=12),
        allow_past=True,
    )
    await oncall.create_shift(
        session,
        admin_id=crew[1].id,
        role="backup",
        starts_at=start,
        ends_at=start + timedelta(hours=12),
        allow_past=True,
    )

    target = await escalation.resolve_target(session, "next", now())

    assert target.admin_id == crew[1].id
    assert "Запасной" in target.label
