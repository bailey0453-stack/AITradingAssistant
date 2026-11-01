"""Paper hedge performance endpoints (SIMULATED; model evaluation only)."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.database import get_db
from app.models import Recommendation
from app.routers.recommendations import serialize_recommendation
from app.services import calibration_preview, exit_strategy_comparison, research_lab

router = APIRouter(prefix="/performance", tags=["performance"])


@router.get("/monthly")
def monthly(db: Session = Depends(get_db)) -> dict:
    return research_lab.monthly_performance(db)


@router.get("/summary")
def summary(db: Session = Depends(get_db)) -> dict:
    """Overall paper hedge summary (SIMULATED PAPER PERFORMANCE)."""
    return research_lab.paper_hedge_performance(db)


@router.get("/calibration-preview")
def calibration_preview_endpoint(db: Session = Depends(get_db)) -> dict:
    """Preview execution-based calibration against stored paper outcomes."""
    return calibration_preview.build_calibration_preview(db)


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


@router.get("/exit-strategy-comparison")
def exit_strategy_comparison_endpoint(
    persist: bool = Query(default=True),
    db: Session = Depends(get_db),
) -> dict:
    """Compare fixed-horizon 1d vs first-net-profit exits (simulated only)."""
    return exit_strategy_comparison.compare_exit_strategies(db, persist=persist)
