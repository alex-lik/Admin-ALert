"""Точка входа API."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.api.router import api_router
from app.config import get_settings
from app.db import dispose_engine


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    # Здесь же — подключение к Redis и прогрев профилей эскалации из БД.
    yield
    await dispose_engine()


def create_app() -> FastAPI:
    settings = get_settings()
    app = FastAPI(
        title=settings.app_name,
        version=settings.version,
        description="Приём алертов, дедупликация и эскалация на дежурных админов",
        lifespan=lifespan,
    )
    # Настройки в состоянии приложения, а не в замыкании роутеров: webhook
    # Telegram проверяет секрет и должен видеть ровно ту конфигурацию, с
    # которой сервер запущен, а не копию, снятую при импорте.
    app.state.settings = settings
    app.include_router(api_router, prefix="/api/v1")
    return app


app = create_app()
