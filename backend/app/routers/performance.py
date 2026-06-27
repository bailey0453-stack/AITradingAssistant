"""Paper hedge performance endpoints (SIMULATED; model evaluation only)."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.database import get_db
from app.models import Recommendation
from app.routers.recommendations import serialize_recommendation
from app.services import research_lab

router = APIRouter(prefix="/performance", tags=["performance"])


@router.get("/monthly")
def monthly(db: Session = Depends(get_db)) -> dict:
    return research_lab.monthly_performance(db)


@router.get("/summary")
def summary(db: Session = Depends(get_db)) -> dict:
    """Overall paper hedge summary (SIMULATED PAPER PERFORMANCE)."""
    return research_lab.paper_hedge_performance(db)


@router.get("/recommendations")
def recommendations(
    limit: int = Query(default=50, ge=1, le=500),
    db: Session = Depends(get_db),
) -> dict:
    """Recent recommendations with their evaluated outcomes (incl. paper P/L)."""
    rows = db.execute(
        select(Recommendation).order_by(Recommendation.created_at.desc()).limit(limit)
    ).scalars().all()
    return {
        "label": "SIMULATED PAPER PERFORMANCE",
        "count": len(rows),
        "recommendations": [serialize_recommendation(r, with_outcomes=True) for r in rows],
    }
