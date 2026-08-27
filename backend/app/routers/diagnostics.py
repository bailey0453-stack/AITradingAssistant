"""Read-only storage diagnostics.

``GET /diagnostics/db`` reports which database is actually in use (persistent
Postgres vs. ephemeral SQLite) and high-level row counts. This is the quickest
way to confirm that recommendations / evaluations / job runs are accumulating in
durable storage rather than being lost on each serverless cold start.

No secrets are returned — only the coarse database kind, never the URL.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, Query, Response
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.database import database_is_persistent, database_kind, get_db
from app.models import (
    JobRun,
    MarketSnapshot,
    Recommendation,
    RecommendationOutcome,
)

from app.services.grade_diagnostics import grade_calibration_report
from app.services.research_database_status import research_database_status

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/diagnostics", tags=["diagnostics"])


@router.on_event("startup")
def _enable_live_fix_dashboard_polling() -> None:
    """Refresh the FIX card every 2.5s without requiring a manual page reload."""
    try:
        import app.main as main

        marker = "</body>"
        script = "<script>setInterval(function(){if(typeof loadFixMarketData==='function'){loadFixMarketData();}},2500);</script>"
        if script not in main.DASHBOARD_HTML and marker in main.DASHBOARD_HTML:
            main.DASHBOARD_HTML = main.DASHBOARD_HTML.replace(marker, script + marker)
    except Exception:  # noqa: BLE001
        logger.exception("Unable to enable live FIX dashboard polling")


def _count(db: Session, model) -> int:
    return int(db.execute(select(func.count()).select_from(model)).scalar() or 0)


@router.get("/db")
def db_diagnostics(db: Session = Depends(get_db)) -> dict:
    """Storage diagnostics: active database type + durable row counts."""
    kind = database_kind()
    persistent = database_is_persistent()

    # "Evaluated" = recommendations that have at least one scored outcome.
    evaluated = int(
        db.execute(
            select(func.count(func.distinct(RecommendationOutcome.recommendation_id)))
        ).scalar()
        or 0
    )

    return {
        "database_type": kind,  # "postgres" | "sqlite"
        "persistent": persistent,
        "storage_note": (
            "Persistent storage — recommendation history survives redeploys and "
            "cold starts."
            if persistent
            else "Ephemeral SQLite — data is per-instance and lost on cold starts."
        ),
        "total_recommendations": _count(db, Recommendation),
        "total_evaluated_recommendations": evaluated,
        "total_market_snapshots": _count(db, MarketSnapshot),
        "total_job_runs": _count(db, JobRun),
    }


@router.get("/grade-calibration")
def grade_calibration(
    db: Session = Depends(get_db),
    limit: int = Query(default=100, ge=1, le=500),
) -> dict:
    """Before/after grade + confidence distributions (Phase A calibration).

    Replays stored analysis snapshots through legacy (pre-Phase-A) and v2
    grading without mutating any records.
    """
    return grade_calibration_report(db, limit=limit)


@router.get("/research-database")
def research_database_panel(db: Session = Depends(get_db)) -> dict:
    """Read-only Research Database Status for the dashboard health panel."""
    return research_database_status(db)


@router.get("/fix")
def fix_market_data_diagnostics(response: Response) -> dict:
    """Read-only Centroid/GFC FIX market-data session status (no secrets)."""
    from app.services.fix.provider import get_fix_diagnostics

    response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    response.headers["Pragma"] = "no-cache"
    payload = get_fix_diagnostics()
    session = payload.get("session") or {}
    # The dashboard historically labeled len(symbols) as quote count.  Show the
    # actual number of market-data updates received by the persistent worker.
    if session.get("quotes_received_count") is not None:
        payload["quote_count"] = session["quotes_received_count"]
    return payload
