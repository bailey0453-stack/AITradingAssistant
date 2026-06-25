"""Health check endpoint."""

from datetime import datetime, timezone

from fastapi import APIRouter

from app import __version__
from app.config import get_settings

router = APIRouter(tags=["health"])


@router.get("/health")
def health() -> dict:
    settings = get_settings()
    return {
        "status": "ok",
        "app": settings.app_name,
        "version": __version__,
        "environment": settings.environment,
        "mock_data": settings.is_mock,
        "time": datetime.now(timezone.utc).isoformat(),
    }
