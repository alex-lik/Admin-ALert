"""POST /events — единая точка приёма алертов (задачи 2.3, 2.4, 2.6)."""

import logging
from typing import Annotated

from fastapi import APIRouter, Header, HTTPException, status
from fastapi.responses import JSONResponse

from app.api.deps import SessionDep, resolve_project
from app.schemas.events import EventAccepted, EventStatus, IncomingEvent
from app.services.alerts import AlertError, ingest_event
from app.services.metrics import metrics
from app.services.ratelimit import get_limiter

router = APIRouter()


async def _notify_resolved(alert_id: str) -> None:
    """Отправить «проблема устранена». Ошибки глушим: resolve уже состоялся."""
    from app.config import get_settings
    from app.services import dispatch

    dispatcher = dispatch.make_dispatcher(get_settings())
    try:
        await dispatch.notify_resolved(dispatcher, alert_id)
    except Exception:  # noqa: BLE001 — уведомление не важнее самого факта resolve
        logging.getLogger("admin-alert.events").exception(
            "уведомление о resolved не отправлено: alert=%s", alert_id
        )
    finally:
        await dispatcher.aclose()


@router.post("", response_model=EventAccepted, status_code=status.HTTP_202_ACCEPTED)
async def receive_event(
    event: IncomingEvent,
    session: SessionDep,
    x_api_key: Annotated[str | None, Header()] = None,
) -> EventAccepted | JSONResponse:
    """Принять событие от источника.

    Ответ 202: событие принято в обработку, а не обработано. Источнику не нужно
    ждать, пока админ отреагирует — он получит результат по push.

    Порядок важен: тело запроса валидируется до проверки ключа. Иначе
    источник с неверным payload получил бы 401 и решил бы, что проблема
    в ключе, а не в его собственном запросе.

    Возвращается также JSONResponse с 429 — тип маршрута шире модели ответа,
    и это осознанно: у отказа другой код и другой формат, чем у успеха.
    """
    project = await resolve_project(session, x_api_key)

    # Ограничение частоты — по API-ключу проекта, а не по IP: источники
    # стоят за NAT и в Docker-сетях, где все запросы приходят с одного
    # адреса (задача 2.24).
    limit = await get_limiter().check(project.api_key)
    if not limit.allowed:
        metrics.event_rate_limited(limit.degraded)
        return JSONResponse(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            content={
                "detail": (
                    f"превышен лимит {limit.limit} событий в минуту для проекта {project.name}"
                ),
                "retry_after": limit.retry_after,
            },
            headers={
                "Retry-After": str(limit.retry_after),
                "X-RateLimit-Limit": str(limit.limit),
                "X-RateLimit-Remaining": "0",
            },
        )

    try:
        alert, created = await ingest_event(session, project, event)
    except AlertError as exc:
        # Единственный случай: resolved без истории. Выдумывать алерт
        # не будем — источнику честный 404, а не пустой ответ.
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    except Exception as exc:  # noqa: BLE001
        metrics.increment("adminalert_events_total", {"status": "error"})
        raise exc

    await session.commit()
    metrics.event_accepted(event.status, created)

    # Источник закрыл алерт — сообщаем дежурным «проблема устранена» (задача
    # 2.6). Отдельный сигнал важен: без него ночной дежурный не узнает, что
    # можно спать. Уведомление всегда после коммита и всегда best-effort:
    # упавший Google не должен превращать 202 источника в ошибку.
    if event.status is EventStatus.RESOLVED:
        await _notify_resolved(alert.id)

    return EventAccepted(
        alert_id=alert.id,
        dedup_key=alert.dedup_key,
        status=event.status,
        occurrences=alert.occurrences,
        # duplicate=True означает, что это тот же самый инцидент: эскалация
        # не перезапускается, растёт только счётчик (задача 2.4).
        duplicate=not created,
    )
