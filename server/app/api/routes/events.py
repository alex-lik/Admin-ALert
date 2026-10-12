"""POST /events — единая точка приёма алертов (задачи 2.3, 2.4, 2.6)."""

from typing import Annotated

from fastapi import APIRouter, Header, status

from app.api.deps import SessionDep, resolve_project
from app.schemas.events import EventAccepted, IncomingEvent
from app.services.alerts import ingest_event

router = APIRouter()


@router.post("", response_model=EventAccepted, status_code=status.HTTP_202_ACCEPTED)
async def receive_event(
    event: IncomingEvent,
    session: SessionDep,
    x_api_key: Annotated[str | None, Header()] = None,
) -> EventAccepted:
    """Принять событие от источника.

    Ответ 202: событие принято в обработку, а не обработано. Источнику не нужно
    ждать, пока админ отреагирует — он получит результат по push.

    Порядок важен: тело запроса валидируется до проверки ключа. Иначе
    источник с неверным payload получил бы 401 и решил бы, что проблема
    в ключе, а не в его собственном запросе.
    """
    project = await resolve_project(session, x_api_key)

    alert, created = await ingest_event(session, project, event)
    await session.commit()

    return EventAccepted(
        alert_id=alert.id,
        dedup_key=alert.dedup_key,
        status=event.status,
        occurrences=alert.occurrences,
        # duplicate=True означает, что это тот же самый инцидент: эскалация
        # не перезапускается, растёт только счётчик (задача 2.4).
        duplicate=not created,
    )
