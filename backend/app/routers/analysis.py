"""USD/MXN AI analysis endpoints."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.database import get_db
from app.models import AnalysisSnapshot
from app.routers.market import capture_market_snapshot, serialize_market
from app.services.ai_analysis import get_analyzer

router = APIRouter(prefix="/analysis", tags=["analysis"])


def serialize_analysis(row: AnalysisSnapshot, market: dict | None = None) -> dict:
    return {
        "id": row.id,
        "pair": row.pair,
        "created_at": row.created_at.isoformat() if row.created_at else None,
        "direction": row.direction,
        "confidence": row.confidence,
        "summary": row.summary,
        "key_drivers": row.key_drivers,
        "target": row.target,
        "stretch_target": row.stretch_target,
        "stop": row.stop,
        "momentum_status": row.momentum_status,
        "risk_notes": row.risk_notes,
        "model": row.model,
        "market_snapshot_id": row.market_snapshot_id,
        "market": market,
    }


@router.get("/usdmxn")
def analyze_usdmxn(db: Session = Depends(get_db)) -> dict:
    """Capture a fresh market snapshot, run AI analysis, store and return it."""
    snapshot, market, news = capture_market_snapshot(db)

    result = get_analyzer().analyze(market, news)

    analysis = AnalysisSnapshot(
        pair="USDMXN",
        direction=result["direction"],
        confidence=result["confidence"],
        summary=result["summary"],
        key_drivers=result["key_drivers"],
        target=result["target"],
        stretch_target=result["stretch_target"],
        stop=result["stop"],
        momentum_status=result["momentum_status"],
        risk_notes=result["risk_notes"],
        model=result.get("model", "mock-rules-v1"),
        market_snapshot_id=snapshot.id,
    )
    db.add(analysis)
    db.commit()
    db.refresh(analysis)

    return serialize_analysis(analysis, market=serialize_market(snapshot))


@router.get("/usdmxn/history")
def analysis_history(
    limit: int = Query(default=20, ge=1, le=200),
    db: Session = Depends(get_db),
) -> dict:
    rows = db.execute(
        select(AnalysisSnapshot)
        .where(AnalysisSnapshot.pair == "USDMXN")
        .order_by(AnalysisSnapshot.created_at.desc())
        .limit(limit)
    ).scalars().all()
    return {"count": len(rows), "analyses": [serialize_analysis(r) for r in rows]}
