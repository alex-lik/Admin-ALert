"""Health-check: для балансировщика, для внешнего watchdog (задача 2.23) и для
внутреннего дежурного агента (задача 3.27).
"""

from datetime import UTC, datetime

from fastapi import APIRouter, Response, status
from sqlalchemy import text

from app.config import get_settings
from app.db import SessionLocal, engine

router = APIRouter()


@router.get("/health")
async def health() -> dict[str, object]:
    """Жив ли процесс. Не трогает БД — дешёвый и не создаёт зависимостей."""
    settings = get_settings()
    return {
        "status": "ok",
        "app": settings.app_name,
        "version": settings.version,
        "time": datetime.now(UTC).isoformat(),
    }


@router.get("/metrics")
async def prometheus_metrics() -> Response:
    """Метрики в текстовом формате Prometheus.

    Отдельный эндпоинт, а не часть `/health`: сборщик опрашивает часто, а
    `/health` нужен балансировщику и должен оставаться дешёвым.
    """
    from app.services.metrics import metrics

    return Response(content=metrics.render(), media_type="text/plain; version=0.0.4")


@router.get("/health/ready")
async def readiness(response: Response) -> dict[str, object]:
    """Готов ли процесс принимать события.

    Проверяются две вещи, и обе отвечают на вопрос «работает ли система
    пейджинга», а не «жив ли процесс»:

    1. доступна ли БД;
    2. не копится ли очередь просроченных алертов.

    Вторая важнее первой и неочевидна: при зависшем воркере процесс совершенно
    здоров, `/health` зелёный, а пейджинг не идёт. Сообщить об этом некому —
    некому принимать алерты. Поэтому очередь входит в готовность, и ответ 503
    уводит на проблему балансировщик и внешний watchdog.

    Статус считается по флагам, а не по `response.status_code`: у инъектированного
    в FastAPI объекта `Response` код равен `None`, и сравнение с 200 всегда
    давало бы «not_ready».
    """
    ready = True
    database = "ok"

    try:
        async with engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
    except Exception as exc:  # noqa: BLE001 — health-check не должен падать с трейсбеком
        database = f"error: {exc.__class__.__name__}"
        ready = False

    # Просроченные алерты в очереди воркера.
    from app.services.escalation import count_due

    try:
        async with SessionLocal() as session:
            backlog = await count_due(session)
    except Exception as exc:  # noqa: BLE001
        database = f"{database}, очередь: {exc.__class__.__name__}"
        # Отрицательное значение: очередь не удалось посчитать. Это тоже
        # повод не принимать события — состояние неизвестно.
        backlog = -1
        ready = False

    if backlog > get_settings().worker_backlog_alarm_threshold:
        ready = False

    if not ready:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE

    return {
        "status": "ready" if ready else "not_ready",
        "database": database,
        "backlog": backlog,
    }
