"""Движок эскалации: выбор просроченных алертов и выполнение шагов (2.7–2.9).

Принцип из задачи 1.4: таймеры не живут в памяти. У активного алерта есть
`next_action_at` в БД; воркер раз в N секунд выбирает просроченные записи,
выполняет шаг и планирует следующий. Рестарт воркера ничего не теряет.

Что здесь, а что нет:
- здесь: выборка, блокировка строк, адресация шага (2.11), продвижение
  `current_step`, планирование следующего таймера, истечение по лимитам,
  ускорение при молчащем устройстве (2.21);
- не здесь: сама отправка push/Telegram/звонка. За неё отвечает Dispatcher,
  а пока каналы не подключены — NullDispatcher, который честно пишет
  `skipped`, а не выдуманное `delivered`.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Protocol

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.models.entities import (
    Admin,
    Alert,
    AlertStep,
    Device,
    EscalationProfile,
    OnCallShift,
)
from app.models.enums import AlertState
from app.services.alerts import coerce_aware, new_id, now, transition
from app.services.quiet import QuietWindow

logger = logging.getLogger("admin-alert.worker")

DUE_STATES = (AlertState.TRIGGERED, AlertState.NOTIFYING, AlertState.ESCALATED)

# Пауза после последнего шага перед признанием алерта истёкшим: звонок
# должен успеть прозвучать, а админ — снять трубку.
FINAL_GRACE_SECONDS = 300
# Через сколько повторить попытку, если шаг сорвался (нет связи с устройством):
# следующий шаг наступает раньше расписания, эскалация ускоряется.
RETRY_SOON_SECONDS = 30


@dataclass
class Target:
    admin_id: str | None
    label: str
    # Часовой пояс адресата: тихие часы считаются по его времени, а не по
    # серверному. Дежурный в Киеве и администратор во Франкфурте должны иметь
    # разное «23:00».
    timezone: str = "UTC"


class Dispatcher(Protocol):
    async def send(
        self, channel: str, alert: Alert, target: Target, *, step: int
    ) -> tuple[str, str | None]:
        """Отправить шаг по каналу. Возвращает (outcome, note).

        `step` передаётся явно: в момент вызова `alert.current_step` ещё
        указывает на предыдущий шаг — он обновляется только после отправки, на
        случай если отправка сорвётся. Читать номер из алерта значило бы
        показывать дежурному «шаг 1» вместо «шаг 2».
        """
        ...


class NullDispatcher:
    """Заглушка, пока каналы не подключены.

    Пишет `skipped`, а не `delivered`: в аудите не должно быть записей
    о доставке, которой не было. Машина при этом работает полностью —
    шаги продвигаются, таймеры планируются, лимиты срабатывают.
    """

    async def send(
        self, channel: str, alert: Alert, target: Target, *, step: int
    ) -> tuple[str, str | None]:
        del alert, target, step
        return ("skipped", f"канал {channel}: отправка не подключена")


async def get_oncall(session: AsyncSession, role: str, at: datetime) -> OnCallShift | None:
    """Смена, покрывающая момент времени (задача 2.10 — читающая сторона)."""
    result = await session.execute(
        select(OnCallShift)
        .where(OnCallShift.role == role, OnCallShift.starts_at <= at, OnCallShift.ends_at > at)
        .order_by(OnCallShift.starts_at.desc())
    )
    return result.scalars().first()


async def first_active_admin(session: AsyncSession) -> Admin | None:
    result = await session.execute(
        select(Admin).where(Admin.is_active.is_(True)).order_by(Admin.created_at)
    )
    return result.scalars().first()


async def resolve_target(session: AsyncSession, target: str, at: datetime) -> Target:
    """Кому адресован шаг (задача 2.11): текущий, следующий или всем.

    Адресат вычисляется в момент выполнения шага, а не при создании алерта:
    дежурство могло смениться. Если расписание не заполнено, падаем на
    первого активного админа и честно пишем это в метку — лучше пейджить
    хоть кого-то, чем молча никого.
    """
    if target == "all":
        result = await session.execute(select(Admin).where(Admin.is_active.is_(True)))
        admins = list(result.scalars())
        if not admins:
            return Target(None, "некому: нет активных админов")
        return Target(admins[0].id, "всем дежурным", admins[0].timezone)

    role = "primary" if target == "current" else "backup"
    shift = await get_oncall(session, role, at)
    if shift is not None:
        admin = await session.get(Admin, shift.admin_id)
        if admin is not None and admin.is_active:
            who = "Дежурный" if role == "primary" else "Запасной"
            return Target(admin.id, f"{who} ({admin.name})", admin.timezone)

    fallback = await first_active_admin(session)
    if fallback is None:
        return Target(None, "дежурный не назначен")
    return Target(fallback.id, f"{fallback.name} (дежурство не расписано)", fallback.timezone)


async def devices_reachable(
    session: AsyncSession, admin_id: str | None, stale_after: datetime
) -> bool:
    """Есть ли у админа устройство, которое недавно отвечало (задача 2.21).

    Heartbeat без последствий — мёртвая метрика. Здесь она влияет на
    эскалацию: push на молчащий телефон не ждёт своей очереди, а сразу
    считается сорванным, и машина быстрее доходит до звонка.
    """
    if admin_id is None:
        return False
    result = await session.execute(
        select(Device).where(Device.admin_id == admin_id, Device.is_active.is_(True))
    )
    devices = list(result.scalars())
    if not devices:
        return False
    return any(
        d.last_seen_at is not None and coerce_aware(d.last_seen_at) >= stale_after for d in devices
    )


async def poll_once(
    session: AsyncSession,
    dispatch: Dispatcher | None = None,
    *,
    use_lock: bool,
    batch_size: int = 100,
    heartbeat_stale_seconds: int = 900,
    window: QuietWindow | None = None,
) -> int:
    """Один проход воркера. Возвращает число обработанных алертов.

    `use_lock=True` — только для Postgres: строки блокируются через
    `FOR UPDATE SKIP LOCKED`, и второй воркер не возьмёт то же самое
    (задача 2.8). На SQLite блокировки строк нет — там предполагается
    один воркер.
    """
    dispatch = dispatch or NullDispatcher()
    moment = now()

    stmt = (
        select(Alert.id)
        .where(
            Alert.next_action_at.is_not(None),
            Alert.next_action_at <= moment,
            Alert.state.in_(DUE_STATES),
        )
        .order_by(Alert.next_action_at)
        .limit(batch_size)
    )
    if use_lock:
        stmt = stmt.with_for_update(skip_locked=True)

    ids = (await session.execute(stmt)).scalars().all()

    processed = 0
    for alert_id in ids:
        try:
            outcome = await process_due_alert(
                session,
                alert_id,
                dispatch,
                heartbeat_stale_seconds=heartbeat_stale_seconds,
                window=window,
            )
            # deferred — это не обработка: алерт честно перенесён на утро.
            # Считать его обработанным значило бы показывать в мониторинге
            # работу, которой не было.
            if outcome not in ("skipped", "deferred"):
                processed += 1
        except Exception:
            # Один битый алерт не должен останавливать весь проход.
            logger.exception("воркер: алерт %s не обработан", alert_id)
            await session.rollback()
    return processed


async def process_due_alert(
    session: AsyncSession,
    alert_id: str,
    dispatch: Dispatcher,
    *,
    heartbeat_stale_seconds: int = 900,
    final_grace_seconds: int = FINAL_GRACE_SECONDS,
    soon_seconds: int = RETRY_SOON_SECONDS,
    window: QuietWindow | None = None,
) -> str:
    """Обработать один просроченный алерт.

    Возвращает итог: `step:N`, `expired`, `deferred` (тихие часы) или `skipped`
    (алерт уже забрал кто-то другой — нормальная ситуация при двух воркерах).
    """
    moment = now()
    window = window if window is not None else QuietWindow(start_hour=0, end_hour=0)

    # Профиль со шагами нужен движку, project и host — диспетчеру: из них он
    # собирает текст уведомления. Всё грузим явно: ленивая загрузка на
    # AsyncSession запрещена и падает с MissingGreenlet ровно в момент отправки.
    result = await session.execute(
        select(Alert)
        .where(Alert.id == alert_id)
        .options(
            selectinload(Alert.profile).selectinload(EscalationProfile.steps),
            selectinload(Alert.project),
            selectinload(Alert.host),
            selectinload(Alert.check),
        )
    )
    alert = result.scalar_one_or_none()
    if alert is None:
        return "skipped"

    if (
        AlertState(alert.state) not in DUE_STATES
        or alert.next_action_at is None
        or coerce_aware(alert.next_action_at) > moment
    ):
        # Состояние сменилось (ack/resolve) или шаг уже выполнил второй
        # воркер, пока мы читали. Молча уходим — это не ошибка.
        return "skipped"

    if alert.snoozed_until is not None and coerce_aware(alert.snoozed_until) > moment:
        # Отложение ещё действует: таймер должен был стоять дальше.
        # Такое бывает, если snooze поставили после выборки.
        alert.next_action_at = alert.snoozed_until
        await session.commit()
        return "skipped"
    alert.snoozed_until = None

    definitions = sorted(alert.profile.steps, key=lambda d: d.step)
    current = next((d for d in definitions if d.step > alert.current_step), None)

    if current is None or alert.current_step >= alert.profile.max_steps:
        # Шаги кончились или упёрлись в лимит профиля (задача 2.9):
        # дальше тишина, чтобы не было бесконечного цикла звонков.
        await transition(session, alert, AlertState.EXPIRED)
        await session.commit()
        return "expired"

    target = await resolve_target(session, current.target, moment)

    # Читаем флаг ДО отправки: диспетчер открывает свои сессии и может
    # инвалидировать связи этого объекта, а ленивая загрузка на AsyncSession
    # падает с MissingGreenlet. Значение закешировано локально — ниже оно
    # используется повторно.
    respects_quiet_hours = alert.profile.respects_quiet_hours

    # Тихие часы (задача 2.13). Проверяем до выполнения шага и только для
    # профилей, которые их уважают: critical идёт в ночь как обычно.
    if respects_quiet_hours and window.is_quiet(moment, target.timezone):
        until = window.ends_at(moment, target.timezone)
        alert.next_action_at = until
        await session.commit()
        logger.info(
            "тихие часы: алерт %s ждёт до %s (профиль %s)",
            alert.id,
            until.isoformat(),
            alert.profile_id,
        )
        return "deferred"

    outcome: str
    note: str | None
    if current.channel == "push" and not await devices_reachable(
        session, target.admin_id, moment - timedelta(seconds=heartbeat_stale_seconds)
    ):
        # Телефон молчит — push ждать бессмысленно. Шаг помечается
        # сорванным, а следующий наступает раньше расписания: эскалация
        # ускоряется в сторону звонка (задача 2.21).
        outcome, note = (
            "failed",
            "устройство не на связи: heartbeat давно не приходил, шаг пропущен",
        )
    else:
        outcome, note = await dispatch.send(current.channel, alert, target, step=current.step)

    session.add(
        AlertStep(
            id=new_id("stp"),
            alert_id=alert.id,
            step=current.step,
            channel=current.channel,
            target_admin_id=target.admin_id,
            target_label=target.label,
            outcome=outcome,
            note=note,
        )
    )
    alert.current_step = current.step
    await transition(session, alert, AlertState.NOTIFYING)

    if outcome == "failed":
        next_at = moment + timedelta(seconds=soon_seconds)
    else:
        following = next((d for d in definitions if d.step > current.step), None)
        if following is not None and alert.current_step < alert.profile.max_steps:
            planned = coerce_aware(alert.first_seen_at) + timedelta(seconds=following.delay_seconds)
            # Догоняем без ожидания: после рестарта воркера просроченные
            # шаги не должны ждать полного расписания заново.
            next_at = max(planned, moment)
        else:
            next_at = moment + timedelta(seconds=final_grace_seconds)

    # Тихие часы проверяются на каждом шаге, а не только на первом. Иначе
    # цепочка `push → telegram → call` ночью разбудила бы на push, а к утру
    # добралась до звонка: тихие часы получились бы только для первого шага.
    if respects_quiet_hours and window.is_quiet(next_at, target.timezone):
        until = window.ends_at(next_at, target.timezone)
        logger.info(
            "тихие часы: следующий шаг алерта %s перенесён на %s", alert.id, until.isoformat()
        )
        next_at = until

    alert.next_action_at = next_at

    await session.commit()
    return f"step:{current.step}"


async def count_due(session: AsyncSession) -> int:
    """Сколько алертов ждёт обработки. Для мониторинга самого воркера."""
    moment = now()
    return (
        await session.execute(
            select(func.count())
            .select_from(Alert)
            .where(
                Alert.next_action_at.is_not(None),
                Alert.next_action_at <= moment,
                Alert.state.in_(DUE_STATES),
            )
        )
    ).scalar_one()
