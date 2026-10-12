"""HTTP-слой: роутеры собираются в одном месте."""

from fastapi import APIRouter

from app.api.routes import alerts, auth, events, health

api_router = APIRouter()
api_router.include_router(health.router, tags=["health"])
api_router.include_router(auth.router, tags=["auth"])
api_router.include_router(alerts.router, prefix="/alerts", tags=["alerts"])
api_router.include_router(events.router, prefix="/events", tags=["events"])
