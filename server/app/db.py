"""Подключение к БД.

Используется async SQLAlchemy 2.0. В проде — Postgres, для локальных тестов можно
переключить DATABASE_URL на sqlite+aiosqlite.
"""

from collections.abc import AsyncIterator

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.config import get_settings

settings = get_settings()

engine = create_async_engine(settings.database_url, pool_pre_ping=True, future=True)

SessionLocal = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)


async def get_session() -> AsyncIterator[AsyncSession]:
    """FastAPI-зависимость: сессия на запрос."""
    async with SessionLocal() as session:
        yield session


async def dispose_engine() -> None:
    await engine.dispose()
