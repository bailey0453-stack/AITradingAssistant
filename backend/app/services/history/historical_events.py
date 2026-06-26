"""Read + aggregate historical events and their reactions from the DB.

Thin data-access layer over the Phase 4 tables. All reads are resilient (a
failure yields an empty list) so the history endpoints and the analysis
endpoint never break. Seeding is idempotent and uses the mock/sample importer
so the system works with zero paid data providers.
"""

from __future__ import annotations

import logging

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import HistoricalEvent, HistoricalEventReaction

logger = logging.getLogger(__name__)


def _safe(db_call, default):
    try:
        return db_call()
    except Exception:  # noqa: BLE001
        logger.exception("history DB read failed; using empty default.")
        return default


def history_is_empty(db: Session) -> bool:
    return _safe(
        lambda: (db.execute(select(func.count(HistoricalEvent.id))).scalar() or 0) == 0,
        True,
    )


def ensure_history_seeded(db: Session) -> dict:
    """Seed mock/sample history once. Idempotent — skips if events already exist."""
    if not history_is_empty(db):
        return {"seeded": False, "reason": "already populated"}
    # Imported lazily to avoid a circular import (importers -> models -> ...).
    from app.services.history.importers import get_importer

    try:
        result = get_importer("mock").run(db)
        return {"seeded": True, **result}
    except Exception:  # noqa: BLE001
        logger.exception("History sample seed failed.")
        db.rollback()
        return {"seeded": False, "reason": "seed error"}


def reaction_to_dict(r: HistoricalEventReaction, event: HistoricalEvent | None = None) -> dict:
    return {
        "id": r.id,
        "event_id": r.event_id,
        "event_type": r.event_type,
        "event_name": event.event_name if event else None,
        "country": event.country if event else None,
        "release_time": event.release_time.isoformat() if event and event.release_time else None,
        "importance": event.importance if event else None,
        "forecast": event.forecast if event else None,
        "actual": event.actual if event else None,
        "surprise": r.surprise,
        "surprise_z": r.surprise_z,
        "baseline_price": r.baseline_price,
        "windows": {
            "15m": r.ret_15m,
            "1h": r.ret_1h,
            "4h": r.ret_4h,
            "1d": r.ret_1d,
            "3d": r.ret_3d,
            "5d": r.ret_5d,
        },
        "max_favorable_excursion": r.max_favorable_excursion,
        "max_adverse_excursion": r.max_adverse_excursion,
        "time_to_peak_hours": r.time_to_peak_hours,
        "reversal_behavior": r.reversal_behavior,
        "data_completeness": r.data_completeness,
        "context": {
            "dxy": r.dxy,
            "us2y": r.us2y,
            "us10y": r.us10y,
            "oil": r.oil,
            "gold": r.gold,
            "vix": r.vix,
            "sp_futures": r.sp_futures,
            "momentum": r.momentum,
            "regime": r.regime,
            "news_tags": r.news_tags,
        },
        "source": r.source,
        "source_quality": r.source_quality,
    }


def load_reactions(db: Session, event_type: str | None = None, limit: int = 500) -> list[dict]:
    """Load reactions (joined with their event) as dicts for scoring/stats."""
    def _query():
        stmt = (
            select(HistoricalEventReaction, HistoricalEvent)
            .join(HistoricalEvent, HistoricalEvent.id == HistoricalEventReaction.event_id)
        )
        if event_type:
            stmt = stmt.where(HistoricalEventReaction.event_type == event_type)
        stmt = stmt.order_by(HistoricalEvent.release_time.desc()).limit(limit)
        rows = db.execute(stmt).all()
        return [reaction_to_dict(r, ev) for (r, ev) in rows]

    return _safe(_query, [])


def list_events(db: Session, event_type: str | None = None, limit: int = 100) -> list[dict]:
    """List historical events (most recent first), optionally filtered by type."""
    def _query():
        stmt = select(HistoricalEvent)
        if event_type:
            stmt = stmt.where(HistoricalEvent.event_type == event_type)
        stmt = stmt.order_by(HistoricalEvent.release_time.desc()).limit(limit)
        rows = db.execute(stmt).scalars().all()
        return [
            {
                "id": e.id,
                "event_type": e.event_type,
                "event_name": e.event_name,
                "country": e.country,
                "release_time": e.release_time.isoformat() if e.release_time else None,
                "forecast": e.forecast,
                "actual": e.actual,
                "previous": e.previous,
                "surprise": e.surprise,
                "surprise_z": e.surprise_z,
                "importance": e.importance,
                "currency_impact": e.currency_impact,
                "source": e.source,
                "source_quality": e.source_quality,
            }
            for e in rows
        ]

    return _safe(_query, [])
