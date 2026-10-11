"""HTTP-слой: роутеры собираются в одном месте."""

from fastapi import APIRouter

from app.api.routes import events, health

api_router = APIRouter()
api_router.include_router(health.router, tags=["health"])
api_router.include_router(events.router, prefix="/events", tags=["events"])
