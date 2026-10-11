"""Health-check: для балансировщика, для внешнего watchdog (задача 2.23) и для
внутреннего дежурного агента (задача 3.27).
"""

from datetime import UTC, datetime

from fastapi import APIRouter, Response, status
from sqlalchemy import text

from app.config import get_settings
from app.db import engine

router = APIRouter()


@router.get("/health")
async def health() -> dict[str, object]:
    """Жив ли процесс. Не трогает БД — дешёвый и не создаёт зависимостей."""
    return {
        "status": "ok",
        "app": get_settings().app_name,
        "version": get_settings().version,
        "time": datetime.now(UTC).isoformat(),
    }


@router.get("/health/ready")
async def readiness(response: Response) -> dict[str, object]:
    """Готов ли процесс принимать события: проверяем БД."""
    try:
        async with engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
        database = "ok"
    except Exception as exc:  # noqa: BLE001 — health-check не должен падать с трейсбеком
        database = f"error: {exc.__class__.__name__}"
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE

    return {"status": "ready" if database == "ok" else "not_ready", "database": database}
