"""Логика алертов: приём событий, дедупликация, переходы состояний (2.3–2.6, 2.14)."""

import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import ColumnElement, func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.models.entities import Alert, AlertStep, Check, Host, Project
from app.models.enums import AlertState, Severity, can_transition
from app.schemas.events import IncomingEvent


class AlertError(RuntimeError):
    pass


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:16]}"


def now() -> datetime:
    return datetime.now(UTC)


# ───────────────────────────── Справочники ─────────────────────────────


async def upsert_host(session: AsyncSession, project: Project, name: str) -> Host:
    """Найти или создать хоста.

    Источник алертов не обязан где-то регистрировать свои хосты, поэтому
    хост создаётся на лету. SELECT + INSERT вместо upsert-конструкции:
    так одинаково работает и на Postgres, и на SQLite, а конфликт под
    уникальностью `id` здесь невозможен — id всегда новый.
    """
    result = await session.execute(
        select(Host).where(Host.name == name, Host.project_id == project.id)
    )
    host = result.scalar_one_or_none()
    if host is not None:
        return host

    host = Host(id=new_id("hst"), name=name, project_id=project.id)
    session.add(host)
    await session.flush()
    return host


async def upsert_check(
    session: AsyncSession,
    project: Project,
    host: Host,
    key: str,
    default_severity: Severity,
) -> Check:
    """Найти или создать проверку. Уникальность — пара (project, key)."""
    result = await session.execute(
        select(Check).where(Check.project_id == project.id, Check.key == key)
    )
    check = result.scalar_one_or_none()
    if check is None:
        check = Check(
            id=new_id("chk"),
            project_id=project.id,
            host_id=host.id,
            key=key,
            description=None,
            default_severity=default_severity,
        )
        session.add(check)
        await session.flush()
    return check


# ───────────────────────────── Приём событий ─────────────────────────────


async def ingest_event(
    session: AsyncSession,
    project: Project,
    event: IncomingEvent,
) -> tuple[Alert, bool]:
    """Обработать входящее событие.

    Возвращает `(alert, created)`. `created=False` означает, что алерт с таким
    `dedup_key` уже шёл: инцидент тот же, эскалация заново не запускается,
    растёт только счётчик (задача 2.4).
    """
    dedup_key = event.resolved_dedup_key

    result = await session.execute(
        select(Alert).where(
            Alert.dedup_key == dedup_key,
            Alert.state.notin_([AlertState.RESOLVED, AlertState.EXPIRED]),
        )
    )
    alert = result.scalar_one_or_none()

    if alert is not None:
        # Повтор по dedup_key. Ключевое: current_step и next_action_at не
        # трогаем, иначе шторм собырий бесконечно откладывал бы эскалацию.
        alert.occurrences += 1
        alert.last_seen_at = now()
        await session.flush()
        return alert, False

    host = await upsert_host(session, project, event.host)
    check = await upsert_check(session, project, host, event.check, event.severity)

    severity = event.severity
    profile_id = await resolve_profile_id(session, project, severity)

    alert = Alert(
        id=new_id("alt"),
        project_id=project.id,
        host_id=host.id,
        check_id=check.id,
        dedup_key=dedup_key,
        profile_id=profile_id,
        severity=severity,
        state=AlertState.TRIGGERED,
        summary=event.summary,
        detail=None,
        runbook_url=event.runbook_url or check.runbook_url,
        occurrences=1,
        first_seen_at=now(),
        last_seen_at=now(),
        current_step=0,
        next_action_at=None,
    )
    session.add(alert)
    await session.flush()
    return alert, True


async def resolve_profile_id(session: AsyncSession, project: Project, severity: Severity) -> str:
    """Выбрать профиль эскалации (задачи 1.2 и 2.12).

    VIP-проект стартует с агрессивного профиля независимо от severity:
    цена ошибки там выше, поэтому и реакция должна быть быстрее.
    """
    if project.is_vip:
        return "critical-vip"
    if project.default_profile:
        return project.default_profile
    return "critical" if severity is Severity.CRITICAL else "warning"


# ───────────────────────────── Переходы состояний ─────────────────────────────


async def transition(
    session: AsyncSession,
    alert: Alert,
    target: AlertState,
    *,
    next_action_at: datetime | None = None,
    note: str | None = None,
) -> Alert:
    """Перевести алерт в новое состояние с проверкой допустимости перехода."""
    current = AlertState(alert.state)

    if current == target:
        return alert

    if not can_transition(current, target):
        raise AlertError(f"недопустимый переход {current} → {target} для алерта {alert.id}")

    alert.state = target
    alert.next_action_at = next_action_at

    if target is AlertState.ACKED:
        alert.acked_at = now()

    if target in (AlertState.RESOLVED, AlertState.ACKED, AlertState.EXPIRED):
        # Таймеры гасим сразу: иначе воркер через 5 секунд выполнит ещё шаг
        # по алерту, который уже закрыт.
        alert.next_action_at = None

    await session.flush()
    del note
    return alert


async def ack_alert(
    session: AsyncSession,
    alert: Alert,
    admin_id: str,
    admin_name: str,
    comment: str | None = None,
) -> list[str]:
    """Подтвердить алерт и остановить всю эскалацию (задачи 2.14 и 2.15).

    Возвращает список того, что остановлено, — клиенту есть что показать.
    """
    if AlertState(alert.state) is AlertState.RESOLVED:
        raise AlertError("алерт уже устранён")

    stopped: list[str] = []

    if alert.next_action_at is not None:
        stopped.append(f"таймер следующего шага ({alert.next_action_at:%H:%M:%S})")
    if AlertState(alert.state) in (AlertState.NOTIFYING, AlertState.ESCALATED):
        stopped.append("повторные push-уведомления")
        stopped.append("звонок")
        stopped.append("напоминания в Telegram")
    if alert.snoozed_until is not None:
        stopped.append("отложение")
        alert.snoozed_until = None

    alert.acked_at = now()
    alert.acked_by_id = admin_id

    await transition(session, alert, AlertState.ACKED)

    # Комментарий админа — часть аудита (задача 2.22). Без него в истории
    # остаётся голое «подтверждено», и через месяц непонятно, что происходило.
    # Один шаг, а не два: ack с комментарием — это одно событие.
    note = "Подтверждено из приложения"
    if comment:
        note = f"{note}. {comment}"

    session.add(
        AlertStep(
            id=new_id("stp"),
            alert_id=alert.id,
            step=alert.current_step + 1,
            channel="push",
            target_admin_id=admin_id,
            target_label=admin_name,
            outcome="acked",
            note=note,
        )
    )
    await session.flush()
    return stopped


async def snooze_alert(session: AsyncSession, alert: Alert, minutes: int) -> datetime:
    """Отложить эскалацию.

    Ограничения намеренно жёсткие (задача 2.14): иначе дежурный откладывает
    всё подряд и система перестаёт пейджить.
    """
    if AlertState(alert.state) is AlertState.RESOLVED:
        raise AlertError("алерт уже устранён")

    if minutes > 30:
        raise AlertError("отложить дольше 30 минут нельзя")

    until = now() + timedelta(minutes=minutes)
    alert.snoozed_until = until
    alert.next_action_at = until

    session.add(
        AlertStep(
            id=new_id("stp"),
            alert_id=alert.id,
            step=alert.current_step,
            channel="snooze",
            target_label="дежурный",
            outcome="delivered",
            note=f"Отложено на {minutes} мин",
        )
    )
    await session.flush()
    return until


async def resolve_alert(session: AsyncSession, alert: Alert, note: str | None = None) -> Alert:
    """Устранить алерт: остановить таймеры (задача 2.6)."""
    return await transition(session, alert, AlertState.RESOLVED, note=note)


# ───────────────────────────── Выборка ─────────────────────────────


async def list_alerts(
    session: AsyncSession,
    *,
    state: AlertState | None = None,
    only_active: bool = False,
    limit: int = 50,
    offset: int = 0,
) -> tuple[list[Alert], int, int, int]:
    """Список алертов со счётчиками.

    Сортировка повторяет клиентскую: активные → VIP → критичность → время.
    Разница в том, что дежурный видит на телефоне, должна совпадать с тем,
    что сервер отдаёт, иначе список «прыгает» после синхронизации.
    """
    conditions: list[ColumnElement[bool]] = []
    if only_active:
        conditions.append(
            Alert.state.notin_([AlertState.RESOLVED, AlertState.EXPIRED, AlertState.ACKED])
        )
    elif state is not None:
        conditions.append(Alert.state == state)

    stmt = select(Alert).options(
        selectinload(Alert.project),
        selectinload(Alert.host),
        selectinload(Alert.check),
        selectinload(Alert.acked_by),
    )
    if conditions:
        stmt = stmt.where(*conditions)

    result = await session.execute(
        stmt.order_by(Alert.last_seen_at.desc()).limit(limit).offset(offset)
    )
    alerts = list(result.scalars().unique())

    total_stmt = select(func.count()).select_from(Alert)
    for cond in conditions:
        total_stmt = total_stmt.where(cond)
    total = (await session.execute(total_stmt)).scalar_one()

    active_cond = Alert.state.notin_([AlertState.RESOLVED, AlertState.EXPIRED, AlertState.ACKED])
    active_count = (
        await session.execute(select(func.count()).select_from(Alert).where(active_cond))
    ).scalar_one()
    critical_count = (
        await session.execute(
            select(func.count())
            .select_from(Alert)
            .where(active_cond, Alert.severity == Severity.CRITICAL)
        )
    ).scalar_one()

    return alerts, total, active_count, critical_count
