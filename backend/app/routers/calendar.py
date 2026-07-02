"""Economic calendar endpoints."""

from __future__ import annotations

from fastapi import APIRouter, Query

from app.services.calendar import get_calendar_provider

router = APIRouter(prefix="/calendar", tags=["calendar"])


@router.get("/upcoming")
def upcoming(limit: int = Query(default=20, ge=1, le=100)) -> dict:
    """Upcoming tracked economic events."""
    provider = get_calendar_provider()
    events = provider.get_upcoming(limit=limit)
    source = provider.source
    if source == "official":
        source = "live"
    return {
        "count": len(events),
        "provider": source,
        "status": getattr(provider, "status", "MOCK"),
        "coverage_gaps": getattr(provider, "coverage_gaps", []),
        "events": events,
    }


@router.get("/released")
def released(limit: int = Query(default=20, ge=1, le=100)) -> dict:
    """Recently released tracked economic events."""
    provider = get_calendar_provider()
    events = provider.get_recent_released(limit=limit)
    source = provider.source
    if source == "official":
        source = "live"
    return {
        "count": len(events),
        "provider": source,
        "status": getattr(provider, "status", "MOCK"),
        "coverage_gaps": getattr(provider, "coverage_gaps", []),
        "events": events,
    }
