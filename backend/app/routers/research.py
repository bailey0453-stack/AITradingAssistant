"""AI Research Lab endpoints (self-evaluation analytics; read-only & fast)."""

from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.database import get_db
from app.services import research_lab

router = APIRouter(prefix="/research", tags=["research"])


@router.get("/summary")
def summary(db: Session = Depends(get_db)) -> dict:
    return research_lab.research_summary(db)


@router.get("/calibration")
def calibration(db: Session = Depends(get_db)) -> dict:
    return research_lab.calibration(db)


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
