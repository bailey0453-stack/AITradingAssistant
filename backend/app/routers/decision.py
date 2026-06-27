"""Decision Quality endpoints (Phase 5.3).

Read-only views over the latest stored analysis and scored outcomes:
- GET /decision/quality              — trade quality, should-trade-now, R/R, EV
- GET /decision/selective-performance — "if we only traded the top X% ..."
- GET /decision/current-context      — similar-recommendation track record
"""

from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.database import get_db
from app.models import AnalysisSnapshot
from app.services import decision_quality

router = APIRouter(prefix="/decision", tags=["decision"])

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
