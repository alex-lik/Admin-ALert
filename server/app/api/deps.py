"""Зависимости FastAPI: текущий админ, сессия, API-ключ проекта."""

from typing import Annotated

from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_session
from app.models.entities import Admin, Project
from app.services import auth

bearer_scheme = HTTPBearer(auto_error=False)

SessionDep = Annotated[AsyncSession, Depends(get_session)]


async def current_admin(
    session: SessionDep,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer_scheme)],
) -> Admin:
    """Админ из Bearer-токена."""
    if credentials is None or not credentials.credentials:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Нет токена доступа",
            headers={"WWW-Authenticate": "Bearer"},
        )

    try:
        payload = auth.decode_token(credentials.credentials, expected_type="access")
    except auth.AuthError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=str(exc),
            headers={"WWW-Authenticate": "Bearer"},
        ) from exc

    admin = await auth.get_admin_by_id(session, str(payload["sub"]))
    if admin is None or not admin.is_active:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="Админ не найден или отключён"
        )

    return admin


CurrentAdmin = Annotated[Admin, Depends(current_admin)]


async def resolve_project(session: AsyncSession, x_api_key: str | None) -> Project:
    """Найти проект по ключу из заголовка X-API-Key (задача 2.3).

    Это функция, а не зависимость FastAPI, и это сделано намеренно.
    Как зависимость она решалась бы ДО валидации тела запроса, и источник
    получал бы 401 на пустом `summary` — то есть решил бы, что у него
    неверный API-ключ, и полез бы чинить не то. Порядок должен быть:
    сначала форма, потом ключ.
    """
    if not x_api_key:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="Не передан заголовок X-API-Key"
        )

    result = await session.execute(select(Project).where(Project.api_key == x_api_key))
    project = result.scalar_one_or_none()

    if project is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Неизвестный API-ключ")

    return project
