"""POST /events — единая точка приёма алертов (задачи 2.3, 2.4, 2.6).

Каркас: валидация по единой схеме, ответ 202. Логика дедупликации и создания
алерта будет добавлена вместе с моделями.
"""

from fastapi import APIRouter, Header, HTTPException, status

from app.schemas.events import EventAccepted, IncomingEvent

router = APIRouter()


@router.post("", response_model=EventAccepted, status_code=status.HTTP_202_ACCEPTED)
async def receive_event(
    event: IncomingEvent,
    x_api_key: str | None = Header(default=None, description="API-ключ проекта"),
) -> EventAccepted:
    """Принять событие от источника.

    Валидация — по схеме IncomingEvent. Ответ 202: событие принято в обработку,
    результат доставки клиент узнаёт через push, а не через этот ответ.
    """
    if not x_api_key:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Не передан заголовок X-API-Key",
        )

    # TODO(2.3): проверить ключ против проекта и залогировать отказ.
    # TODO(2.4): дедупликация по resolved_dedup_key — обновить occurrences,
    #             но не перезапускать уже идущую эскалацию.
    # TODO(2.6): при status=resolved — остановить таймеры и разослать «проблема устранена».
    return EventAccepted(
        alert_id="pending",
        dedup_key=event.resolved_dedup_key,
        status=event.status,
        occurrences=1,
        duplicate=False,
    )
