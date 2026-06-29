"""Read-only storage diagnostics.

``GET /diagnostics/db`` reports which database is actually in use (persistent
Postgres vs. ephemeral SQLite) and high-level row counts. This is the quickest
way to confirm that recommendations / evaluations / job runs are accumulating in
durable storage rather than being lost on each serverless cold start.

No secrets are returned — only the coarse database kind, never the URL.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.database import database_is_persistent, database_kind, get_db
from app.models import (
    JobRun,
    MarketSnapshot,
    Recommendation,
    RecommendationOutcome,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/diagnostics", tags=["diagnostics"])


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
