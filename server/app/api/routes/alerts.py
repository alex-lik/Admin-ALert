"""HTTP-слой алертов: список, карточка, ack / snooze / reassign (задачи 2.14, 3.2)."""

from typing import Annotated

from fastapi import APIRouter, HTTPException, Path, Query, status
from sqlalchemy.orm import selectinload

from app.api.deps import CurrentAdmin, SessionDep
from app.models.entities import Alert
from app.models.enums import AlertState
from app.schemas.alerts import (
    AckRequest,
    AckResponse,
    AlertBrief,
    AlertDetail,
    AlertListResponse,
    AlertStepOut,
    CheckOut,
    HostOut,
    ProjectOut,
    ReassignRequest,
    SnoozeRequest,
)
from app.services import alerts as alerts_service

router = APIRouter()


def _to_brief(alert: Alert) -> AlertBrief:
    return AlertBrief(
        id=alert.id,
        # model_validate, а не прямая передача объекта: связи — ORM-сущности,
        # а наружу уходят только поля схемы. Прямая передача раскрыла бы
        # наружу лишнее и перестала бы работать при изменении модели.
        project=ProjectOut.model_validate(alert.project),
        host=HostOut.model_validate(alert.host),
        check=CheckOut.model_validate(alert.check),
        severity=alert.severity,
        state=alert.state,
        summary=alert.summary,
        dedup_key=alert.dedup_key,
        profile=alert.profile_id,
        occurrences=alert.occurrences,
        first_seen_at=alert.first_seen_at,
        last_seen_at=alert.last_seen_at,
        next_action_at=alert.next_action_at,
        acked_at=alert.acked_at,
        acked_by=alert.acked_by.name if alert.acked_by else None,
    )


@router.get("", response_model=AlertListResponse)
async def list_alerts_route(
    session: SessionDep,
    _admin: CurrentAdmin,
    state: Annotated[AlertState | None, Query()] = None,
    only_active: Annotated[bool, Query()] = False,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> AlertListResponse:
    """Список алертов. Основной эндпоинт для экрана дежурного."""
    found, total, active_count, critical_count = await alerts_service.list_alerts(
        session, state=state, only_active=only_active, limit=limit, offset=offset
    )
    return AlertListResponse(
        items=[_to_brief(a) for a in found],
        total=total,
        active_count=active_count,
        critical_count=critical_count,
    )


@router.get("/{alert_id}", response_model=AlertDetail)
async def get_alert_route(
    session: SessionDep,
    _admin: CurrentAdmin,
    alert_id: Annotated[str, Path()],
) -> AlertDetail:
    """Карточка алерта с историей эскалации."""
    alert = await _load_alert(session, alert_id)
    brief = _to_brief(alert)

    return AlertDetail(
        **brief.model_dump(),
        detail=alert.detail,
        runbook_url=alert.runbook_url,
        snoozed_until=alert.snoozed_until,
        escalation_history=[
            AlertStepOut(
                id=step.id,
                step=step.step,
                channel=step.channel,
                target=step.target_label,
                at=step.created_at,
                outcome=step.outcome,
                note=step.note,
            )
            for step in alert.steps
        ],
    )


@router.post("/{alert_id}/ack", response_model=AckResponse, status_code=status.HTTP_200_OK)
async def ack_alert_route(
    session: SessionDep,
    admin: CurrentAdmin,
    alert_id: Annotated[str, Path()],
    payload: AckRequest | None = None,
) -> AckResponse:
    """Подтвердить алерт: остановить push-повторы, звонок и Telegram (2.15)."""
    alert = await _load_alert(session, alert_id)

    try:
        stopped = await alerts_service.ack_alert(
            session, alert, admin.id, admin.name, comment=payload.comment if payload else None
        )
    except alerts_service.AlertError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc

    # Коммит обязателен. Без него сессия закрывается с откатом: ack
    # возвращается успешным, а в базе остаётся triggered — эскалация
    # продолжалась бы, и дежурный получил бы второй звонок по уже
    # подтверждённому алерту.
    await session.commit()
    await session.refresh(alert)

    # acked_at проставляется внутри ack_alert. None здесь означал бы, что
    # сервис вернул acked, но не записал время — это баг, а не пустое поле.
    if alert.acked_at is None:  # pragma: no cover - защита от регрессии
        raise HTTPException(status_code=500, detail="ack не записал acked_at")

    return AckResponse(
        alert_id=alert.id,
        state=alert.state,
        acked_at=alert.acked_at,
        acked_by=admin.name,
        stopped=stopped,
    )


@router.post("/{alert_id}/snooze")
async def snooze_alert_route(
    session: SessionDep,
    admin: CurrentAdmin,
    alert_id: Annotated[str, Path()],
    payload: SnoozeRequest,
) -> dict[str, object]:
    """Отложить эскалацию. С жёстким лимитом — задача 2.14."""
    alert = await _load_alert(session, alert_id)

    try:
        until = await alerts_service.snooze_alert(session, alert, payload.minutes)
    except alerts_service.AlertError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc

    await session.commit()

    return {
        "alert_id": alert.id,
        "snoozed_until": until.isoformat(),
        "minutes": payload.minutes,
    }


@router.post("/{alert_id}/reassign")
async def reassign_alert_route(
    session: SessionDep,
    admin: CurrentAdmin,
    alert_id: Annotated[str, Path()],
    payload: ReassignRequest,
) -> dict[str, object]:
    """Передать алерт другому админу.

    Подтверждённый алерт при передаче переоткрывается: чужое подтверждение
    новый владелец не видел и должен подтвердить сам. Таймеры и шаг при этом
    не трогаются — эскалация продолжается с того же места.
    """
    from app.services import auth as auth_service

    alert = await _load_alert(session, alert_id)

    target = await auth_service.get_admin_by_id(session, payload.admin_id)
    if target is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Админ не найден")

    try:
        reopened = await alerts_service.reassign_alert(
            session, alert, target.id, target.name, admin.name, payload.comment
        )
    except alerts_service.AlertError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc

    await session.commit()

    return {
        "alert_id": alert.id,
        "state": alert.state,
        "reassigned_to": target.name,
        "reassigned_by": admin.name,
        "reopened": reopened,
        "comment": payload.comment,
    }


@router.post("/{alert_id}/resolve")
async def resolve_alert_route(
    session: SessionDep,
    _admin: CurrentAdmin,
    alert_id: Annotated[str, Path()],
) -> dict[str, object]:
    """Устранить алерт вручную: остановить таймеры (задача 2.6).

    Основной путь закрытия — событие `resolved` от источника через
    `POST /events`. Ручное закрытие нужно для ложных срабатываний и для
    случаев, когда источник resolved не прислал.
    """
    alert = await _load_alert(session, alert_id)

    try:
        await alerts_service.resolve_alert(session, alert)
    except alerts_service.AlertError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc

    await session.commit()

    return {"alert_id": alert.id, "state": alert.state}


async def _load_alert(session: SessionDep, alert_id: str) -> Alert:
    """Загрузить алерт со связями — без них карточка не соберётся.

    `populate_existing=True` обязателен: если сессия уже держит этот алерт
    (например, его только что изменили в этом же запросе или сессия
    переиспользуется), без этого флага selectinload вернёт закэшированный
    состав шагов, а не текущий из БД. Детальный экран обязан показывать
    факты, а не кэш.
    """
    from sqlalchemy import select

    result = await session.execute(
        select(Alert)
        .where(Alert.id == alert_id)
        .options(
            selectinload(Alert.project),
            selectinload(Alert.host),
            selectinload(Alert.check),
            selectinload(Alert.acked_by),
            selectinload(Alert.steps),
        )
        .execution_options(populate_existing=True)
    )
    alert = result.scalar_one_or_none()

    if alert is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Алерт не найден")

    return alert
