"""AI Research Lab endpoints (self-evaluation analytics; read-only & fast)."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.database import get_db
from app.services import research_lab
from app.services.current_calibration import calibration_for_confidence, calibration_text

router = APIRouter(prefix="/research", tags=["research"])


@router.get("/summary")
def summary(db: Session = Depends(get_db)) -> dict:
    return research_lab.research_summary(db)


@router.get("/pending")
def pending(db: Session = Depends(get_db)) -> dict:
    """Stored vs evaluated counts and pending evaluations by horizon (read-only)."""
    return research_lab.evaluation_progress(db)


@router.get("/calibration")
def calibration(db: Session = Depends(get_db)) -> dict:
    return research_lab.calibration(db)


@router.get("/current-calibration")
def current_calibration(
    confidence: float = Query(..., ge=0.0, le=100.0),
    horizon: str = Query(default="4h", pattern="^(1h|4h|end_of_day|1d|2d|5d)$"),
) -> dict:
    """Measured track record for the current confidence bucket and horizon."""
    result = calibration_for_confidence(confidence, horizon=horizon)
    result["summary"] = calibration_text(result)
    return result


@router.get("/drivers")
def drivers(db: Session = Depends(get_db)) -> dict:
    return research_lab.driver_stats(db)


@router.get("/model-performance")
def model_performance(db: Session = Depends(get_db)) -> dict:
    return research_lab.model_performance(db)


@router.get("/performance")
def performance(db: Session = Depends(get_db)) -> dict:
    """Paper hedge performance (SIMULATED model evaluation only)."""
    return research_lab.paper_hedge_performance(db)
