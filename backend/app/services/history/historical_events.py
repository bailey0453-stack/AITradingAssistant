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

from app.models import (
    HistoricalEvent,
    HistoricalEventReaction,
    HistoricalMarketSnapshot,
    ResearchMarketSnapshot,
    SimilarityMatch,
)
from app.services.history.historical_snapshots import research_snapshot_bounds

logger = logging.getLogger(__name__)

# Reaction provenance that counts as "real" imported/live data (vs sample).
SAMPLE_QUALITIES = {"sample", "mock", "unknown", ""}


def _safe(db_call, default):
    try:
        return db_call()
    except Exception:  # noqa: BLE001
        logger.exception("history DB read failed; using empty default.")
        return default


def _count(db: Session, model) -> int:
    return _safe(lambda: db.execute(select(func.count(model.id))).scalar() or 0, 0)


def history_is_empty(db: Session) -> bool:
    return _count(db, HistoricalEvent) == 0


def reactions_is_empty(db: Session) -> bool:
    return _count(db, HistoricalEventReaction) == 0


def ensure_history_seeded(db: Session) -> dict:
    """Ensure usable history exists, cheaply, on demand.

    Honors ``HISTORY_IMPORTER`` but only auto-runs *lazy-safe* importers
    (``mock``/``csv`` — local, no network) so page loads never trigger an
    expensive provider backfill. Network importers (alphavantage/fred/yahoo)
    are CLI-only; if one is configured, we still seed the mock sample so
    similarity has reactions to match, and rely on the backfill CLI for the
    real import. Always falls back to mock on any failure. Idempotent —
    skips once reactions already exist.
    """
    if not reactions_is_empty(db):
        return {"seeded": False, "reason": "already populated"}

    # Imported lazily to avoid a circular import (importers -> models -> ...).
    from app.config import get_settings
    from app.services.history.importers import get_importer

    configured = (get_settings().history_importer or "mock").lower()
    importer = get_importer(configured)

    # Only auto-run cheap, event-providing importers on demand.
    if importer.lazy_safe and importer.provides_events:
        try:
            result = importer.run_all(db)
            if result.get("reactions"):
                return {"seeded": True, **result}
            logger.info("Importer %r produced no reactions; falling back to mock.", configured)
        except Exception:  # noqa: BLE001
            logger.exception("Configured importer %r failed; falling back to mock.", configured)
            db.rollback()

    # Fallback / network-importer path: seed the self-contained mock sample.
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


def _has_imported_reactions(db: Session) -> bool:
    """True if any reaction is from a real (imported/live) source, not sample."""
    def _query():
        n = db.execute(
            select(func.count(HistoricalEventReaction.id)).where(
                func.lower(HistoricalEventReaction.source_quality).notin_(SAMPLE_QUALITIES)
            )
        ).scalar()
        return (n or 0) > 0

    return _safe(_query, False)


def load_reactions(db: Session, event_type: str | None = None, limit: int = 500) -> list[dict]:
    """Load reactions (joined with their event) as dicts for scoring/stats.

    Real imported/live history takes priority: once any non-sample reaction
    exists, sample reactions are excluded so similarity matches against real
    data only. With sample data alone, everything is returned (unchanged).
    """
    prefer_real = _has_imported_reactions(db)

    def _query():
        stmt = (
            select(HistoricalEventReaction, HistoricalEvent)
            .join(HistoricalEvent, HistoricalEvent.id == HistoricalEventReaction.event_id)
        )
        if event_type:
            stmt = stmt.where(HistoricalEventReaction.event_type == event_type)
        if prefer_real:
            stmt = stmt.where(
                func.lower(HistoricalEventReaction.source_quality).notin_(SAMPLE_QUALITIES)
            )
        stmt = stmt.order_by(HistoricalEvent.release_time.desc()).limit(limit)
        rows = db.execute(stmt).all()
        return [reaction_to_dict(r, ev) for (r, ev) in rows]

    return _safe(_query, [])


# Quality buckets for classifying what kind of data is present.
_BACKFILLED_QUALITIES = {"imported", "official", "vendor_free", "vendor_paid", "backfilled"}


def _distinct_qualities(db: Session, model) -> set[str]:
    def _query():
        rows = db.execute(select(func.distinct(model.source_quality))).scalars().all()
        return {str(q or "").lower() for q in rows}

    return _safe(_query, set())


def _classify(qualities: set[str]) -> str:
    """Map a set of source_quality values to sample | imported | live | none."""
    if not qualities or qualities <= {""}:
        return "none"
    if "live" in qualities:
        return "live"
    if qualities & _BACKFILLED_QUALITIES:
        return "imported"
    return "sample"


def _max_created_at(db: Session, model) -> str | None:
    def _query():
        val = db.execute(select(func.max(model.created_at))).scalar()
        return val.isoformat() if val else None

    return _safe(_query, None)


def history_diagnostics(db: Session) -> dict:
    """Read-only snapshot of what historical data exists and its provenance.

    Never seeds and never raises — reflects the database exactly as-is so an
    operator can verify whether real backfill has actually run.
    """
    from app.config import get_settings
    from app.services.history.importers import get_importer

    counts = {
        "historical_events": _count(db, HistoricalEvent),
        "historical_event_reactions": _count(db, HistoricalEventReaction),
        "historical_market_snapshots": _count(db, HistoricalMarketSnapshot),
        "research_market_snapshots": _count(db, ResearchMarketSnapshot),
        "similarity_matches": _count(db, SimilarityMatch),
    }

    reaction_qualities = _distinct_qualities(db, HistoricalEventReaction)
    snapshot_qualities = _distinct_qualities(db, HistoricalMarketSnapshot)
    research_qualities = _distinct_qualities(db, ResearchMarketSnapshot)
    reaction_class = _classify(reaction_qualities)
    snapshot_class = _classify(snapshot_qualities | research_qualities)

    # Best provenance across both tables drives the headline class.
    rank = {"none": -1, "sample": 0, "imported": 1, "live": 2}
    data_class = max((reaction_class, snapshot_class), key=lambda c: rank[c])

    last_imported = max(
        [t for t in (
            _max_created_at(db, HistoricalEvent),
            _max_created_at(db, HistoricalEventReaction),
            _max_created_at(db, HistoricalMarketSnapshot),
        ) if t],
        default=None,
    )

    configured = (get_settings().history_importer or "mock").lower()
    importer = get_importer(configured)

    warnings: list[str] = []
    if counts["historical_event_reactions"] == 0 and counts["research_market_snapshots"] == 0:
        warnings.append("No historical reactions or research snapshots — history will be seeded on first use.")
    elif reaction_class == "sample" and counts["research_market_snapshots"] == 0:
        warnings.append(
            "Similarity is using SAMPLE data only. Use Import Historical Data on the "
            "dashboard (Research Database panel) to load real history."
        )
    elif counts["research_market_snapshots"] > 0:
        bounds = research_snapshot_bounds(db)
        warnings.append(
            f"Research database active: {bounds.get('total_snapshots', 0)} daily snapshots "
            f"({bounds.get('start_date')} → {bounds.get('end_date')})."
        )
    if snapshot_class == "imported" and reaction_class == "sample":
        warnings.append(
            "Imported market series exist, but similarity still uses sample "
            "reactions. Import events (CSV) to upgrade similarity matching."
        )

    return {
        "active_importer": configured,
        "importer_source_quality": importer.source_quality,
        "importer_provides": {
            "events": importer.provides_events,
            "series": importer.provides_series,
            "lazy_safe": importer.lazy_safe,
        },
        "counts": counts,
        "data_class": data_class,
        "reactions_data_class": reaction_class,
        "snapshots_data_class": snapshot_class,
        "reaction_source_qualities": sorted(q for q in reaction_qualities if q),
        "snapshot_source_qualities": sorted(q for q in snapshot_qualities if q),
        "similarity_uses": (
            "research_market_snapshots"
            if counts["research_market_snapshots"] > 0
            else "historical_event_reactions"
        ),
        "research_bounds": (
            research_snapshot_bounds(db)
            if counts["research_market_snapshots"] > 0
            else None
        ),
        "last_imported": last_imported,
        "is_sample_only": data_class == "sample",
        "warnings": warnings,
    }


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
