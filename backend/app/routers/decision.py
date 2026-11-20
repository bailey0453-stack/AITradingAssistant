"""Decision Quality endpoints (Phase 5.3).

Read-only views over the latest stored analysis and scored outcomes:
- GET /decision/quality              — trade quality, should-trade-now, R/R, EV
- GET /decision/selective-performance — "if we only traded the top X% ..."
- GET /decision/current-context      — similar-recommendation track record
"""

from __future__ import annotations

from functools import lru_cache
from time import time

from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.database import get_db
from app.models import AnalysisSnapshot
from app.services import decision_quality

router = APIRouter(prefix="/decision", tags=["decision"])


@router.get('/rebound')
def rebound_watch(db: Session = Depends(get_db)) -> dict:
    from app.services.rebound_watch import current
    return current(db)


@lru_cache(maxsize=1)
def _short_term_calendar(five_minute_bucket: int) -> dict:
    """Reuse calendar coverage for five minutes; never cache live FIX prices."""
    from app.services.calendar import get_calendar_provider

    calendar = get_calendar_provider()
    try:
        upcoming = calendar.get_upcoming()
        released = getattr(calendar,"get_recent_released",lambda:[])()
        source = getattr(calendar, "source", "unavailable")
        status = getattr(calendar, "status", "ERROR")
    except Exception:
        upcoming, released, source, status = [], [], "unavailable", "ERROR"
    return {
        "context": {"upcoming_events": upcoming, "released_events": released},
        "data_sources": {"calendar": source, "calendar_status": status},
        "calendar_coverage_gaps": getattr(calendar, "coverage_gaps", []),
    }


@router.get("/now")
def short_term_now(db: Session = Depends(get_db)) -> dict:
    """Independent, read-only 20-minute view with a fresh executable quote."""
    from app.services import short_term_forecast
    from app.services.market_hours import get_market_state
    from app.services.short_term_news import snapshot as news_snapshot

    payload = {
        **_short_term_calendar(int(time() // 300)),
        "market_state": {"is_open": get_market_state().is_open},
        "news_watch": news_snapshot(db),
    }
    return short_term_forecast.from_database(db, payload)


@router.get("/opportunity")
def flexible_opportunity_now(db: Session = Depends(get_db)) -> dict:
    from app.services.flexible_opportunity import current
    from app.services.market_hours import get_market_state
    from app.services.short_term_news import snapshot
    payload = {**_short_term_calendar(int(time()//300)),
               "market_state":{"is_open":get_market_state().is_open},"news_watch":snapshot(db)}
    return current(db,payload)


@router.get("/release")
def release_watch_now(db: Session = Depends(get_db)) -> dict:
    from app.services.release_watch import current
    context=_short_term_calendar(int(time() // 300))["context"]
    return current(db, scheduled=context.get("upcoming_events",[])+context.get("released_events",[]))

_NO_ANALYSIS = {
    "available": False,
    "reason": "No analysis stored yet — call GET /analysis/usdmxn first.",
}


def _latest_analysis(db: Session) -> AnalysisSnapshot | None:
    return db.execute(
        select(AnalysisSnapshot)
        .where(AnalysisSnapshot.pair == "USDMXN")
        .order_by(AnalysisSnapshot.created_at.desc())
        .limit(1)
    ).scalars().first()


@router.get("/quality")
def quality(db: Session = Depends(get_db)) -> dict:
    """Decision-quality assessment of the most recent stored recommendation."""
    row = _latest_analysis(db)
    if row is None:
        return _NO_ANALYSIS
    rec = decision_quality.rec_from_snapshot(db, row)
    out = decision_quality.assess_recommendation(db, rec)
    out["available"] = True
    out["analysis_id"] = row.id
    out["created_at"] = row.created_at.isoformat() if row.created_at else None
    out["confidence"] = row.confidence
    return out


@router.get("/selective-performance")
def selective_performance(db: Session = Depends(get_db)) -> dict:
    """Selective-trading analysis over scored, actionable paper outcomes."""
    return decision_quality.selective_performance(db)


@router.get("/current-context")
def current_context(db: Session = Depends(get_db)) -> dict:
    """Model track record for recommendations similar to the latest one."""
    row = _latest_analysis(db)
    if row is None:
        return _NO_ANALYSIS
    rec = decision_quality.rec_from_snapshot(db, row)
    return {
        "available": True,
        "analysis_id": row.id,
        "created_at": row.created_at.isoformat() if row.created_at else None,
        "direction": row.direction,
        "opportunity_grade": row.opportunity_grade,
        "confidence": row.confidence,
        "regime": rec.get("regime"),
        "reward_risk": decision_quality.reward_risk(
            rec["direction"], rec["entry"], rec["target"], rec["stop"]
        ),
        "similar_track_record": decision_quality.similar_track_record(
            db, rec["direction"], rec["grade"], rec["regime"]
        ),
    }
