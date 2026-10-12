"""Логика алертов: приём событий, дедупликация, переходы состояний (2.3–2.6, 2.14)."""

import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import ColumnElement, case, func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.models.entities import (
    Alert,
    AlertStep,
    Check,
    EscalationStepDefinition,
    Host,
    Project,
)
from app.models.enums import AlertState, Severity, can_transition
from app.schemas.events import EventStatus, IncomingEvent


class AlertError(RuntimeError):
    pass


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:16]}"


def now() -> datetime:
    return datetime.now(UTC)


def coerce_aware(value: datetime) -> datetime:
    """Привести метку к UTC-aware.

    SQLite не хранит часовой пояс: прочитанное из dev-базы наивное время
    сравнивать с `now()` напрямую нельзя — упадёт с TypeError. Считаем
    наивное время UTC: мы сами туда только UTC и пишем.
    """
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value


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

    # Источник сообщает, что проблема устранена (задача 2.6).
    if event.status is EventStatus.RESOLVED:
        return await _ingest_resolved(session, dedup_key, alert)

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
    await _arm_first_timer(session, alert)
    return alert, True


async def _arm_first_timer(session: AsyncSession, alert: Alert) -> None:
    """Взвести таймер первого шага сразу при создании алерта.

    Без этого воркер новый алерт никогда не увидит: он выбирает только
    записи с установленным `next_action_at`. Профиль без шагов — вырожденный
    случай: ставим «сейчас», и первый же проход воркера признает такой алерт
    истёкшим вместо того, чтобы оставить его висеть вечно.
    """
    result = await session.execute(
        select(EscalationStepDefinition)
        .where(EscalationStepDefinition.profile_id == alert.profile_id)
        .order_by(EscalationStepDefinition.step)
        .limit(1)
    )
    first = result.scalar_one_or_none()
    if first is None:
        alert.next_action_at = now()
    else:
        alert.next_action_at = alert.first_seen_at + timedelta(seconds=first.delay_seconds)
    await session.flush()


async def _ingest_resolved(
    session: AsyncSession, dedup_key: str, alert: Alert | None
) -> tuple[Alert, bool]:
    """Закрыть алерт по событию resolved: остановить таймеры (задача 2.6).

    `created` всегда False: resolved никогда не создаёт алерт, он только
    закрывает. Если активного алерта нет, возвращаем последний из истории
    для информации — источнику есть на что сослаться, и счётчик при этом
    не растёт. Если и истории нет — источнику честный 404 через AlertError,
    а не выдуманный алерт.
    """
    if alert is None:
        history = await session.execute(
            select(Alert)
            .where(Alert.dedup_key == dedup_key)
            .order_by(Alert.last_seen_at.desc())
            .limit(1)
        )
        previous = history.scalar_one_or_none()
        if previous is None:
            raise AlertError(f"нет алерта с ключом {dedup_key}: закрывать нечего")
        return previous, False

    await transition(session, alert, AlertState.RESOLVED)
    return alert, False


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
    всё подряд и система перестаёт пейджить. Лимита два: не дольше 30 минут
    за раз и не чаще 2 раз в час на алерт — второе считается по записям
    аудита, отдельный счётчик не нужен.
    """
    if AlertState(alert.state) is AlertState.RESOLVED:
        raise AlertError("алерт уже устранён")

    if minutes > 30:
        raise AlertError("отложить дольше 30 минут нельзя")

    hour_ago = now() - timedelta(hours=1)
    recent_snoozes = (
        await session.execute(
            select(func.count())
            .select_from(AlertStep)
            .where(
                AlertStep.alert_id == alert.id,
                AlertStep.channel == "snooze",
                AlertStep.created_at >= hour_ago,
            )
        )
    ).scalar_one()
    if recent_snoozes >= 2:
        raise AlertError("исчерпан лимит откладываний: не больше 2 раз в час на алерт")

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


async def resolve_alert(session: AsyncSession, alert: Alert) -> Alert:
    """Устранить алерт вручную: остановить таймеры (задача 2.6)."""
    return await transition(session, alert, AlertState.RESOLVED)


async def reassign_alert(
    session: AsyncSession,
    alert: Alert,
    target_admin_id: str,
    target_admin_name: str,
    by_admin_name: str,
    comment: str | None = None,
) -> bool:
    """Передать алерт другому админу.

    Возвращает True, если алерт был переоткрыт. Переоткрытие нужно только
    для подтверждённого алерта: чужое подтверждение новый владелец не видел,
    поэтому должен подтвердить сам. Таймеры и шаг при этом не трогаем —
    эскалация продолжается с того же места, меняется только владелец.

    Передача пишется в аудит каналом `reassign`: иначе теряется ответ на
    вопрос «кому сейчас принадлежит алерт».
    """
    if AlertState(alert.state) is AlertState.RESOLVED:
        raise AlertError("алерт уже устранён")

    reopened = False
    if AlertState(alert.state) is AlertState.ACKED:
        alert.acked_at = None
        alert.acked_by_id = None
        await transition(session, alert, AlertState.NOTIFYING)
        reopened = True

    note = f"Передан {target_admin_name} ({by_admin_name})"
    if comment:
        note = f"{note}. {comment}"

    session.add(
        AlertStep(
            id=new_id("stp"),
            alert_id=alert.id,
            step=alert.current_step,
            channel="reassign",
            target_admin_id=target_admin_id,
            target_label=target_admin_name,
            outcome="delivered",
            note=note,
        )
    )
    await session.flush()
    return reopened


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

    stmt = (
        select(Alert)
        .join(Project, Alert.project_id == Project.id)
        .options(
            selectinload(Alert.project),
            selectinload(Alert.host),
            selectinload(Alert.check),
            selectinload(Alert.acked_by),
        )
    )
    if conditions:
        stmt = stmt.where(*conditions)

    # Порядок — как на экране дежурного в клиенте: активные → VIP →
    # критичность → время. Если сервер отдаёт иначе, список «прыгает»
    # после каждой синхронизации.
    stmt = stmt.order_by(
        case(
            (
                Alert.state.in_([AlertState.TRIGGERED, AlertState.NOTIFYING, AlertState.ESCALATED]),
                0,
            ),
            else_=1,
        ),
        case((Project.is_vip.is_(True), 0), else_=1),
        case((Alert.severity == Severity.CRITICAL, 0), else_=1),
        Alert.last_seen_at.desc(),
    )

    result = await session.execute(
        stmt.limit(limit).offset(offset).execution_options(populate_existing=True)
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
