"""USD/MXN AI analysis endpoints."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.database import get_db
from app.models import AnalysisSnapshot
from app.routers.market import capture_market_snapshot, serialize_market
from app.services.ai_analysis import get_analyzer
from app.services.context_builder import build_context, build_timeline

router = APIRouter(prefix="/analysis", tags=["analysis"])


def serialize_analysis(row: AnalysisSnapshot, market: dict | None = None) -> dict:
    return {
        "id": row.id,
        "pair": row.pair,
        "created_at": row.created_at.isoformat() if row.created_at else None,
        "direction": row.direction,
        "trade_score": row.trade_score,
        "market_bias": row.market_bias,
        "confidence": row.confidence,
        "momentum_status": row.momentum_status,
        "historical_similarity": row.historical_similarity,
        "risk_level": row.risk_level,
        "summary": row.summary,
        "key_drivers": row.key_drivers,
        "entry": row.entry,
        "target": row.target,
        "stretch_target": row.stretch_target,
        "stop": row.stop,
        "expected_move": row.expected_move,
        "expected_duration": row.expected_duration,
        "invalidation_level": row.invalidation_level,
        "risk_notes": row.risk_notes,
        "timeline": row.timeline,
        "model": row.model,
        "market_snapshot_id": row.market_snapshot_id,
        "market": market,
    }


@router.get("/usdmxn")
def analyze_usdmxn(db: Session = Depends(get_db)) -> dict:
    """Capture market + news + calendar context, analyze, store, and return."""
    snapshot, market, news = capture_market_snapshot(db)

    context = build_context(db, market, fresh_news=news)
    timeline = build_timeline(db, context)

    result = get_analyzer().analyze(
        market,
        news=context["recent_news"],
        calendar=context["upcoming_events"] + context["released_events"],
        recent_analyses=context["recent_analyses"],
    )

    analysis = AnalysisSnapshot(
        pair="USDMXN",
        direction=result["direction"],
        trade_score=result["trade_score"],
        market_bias=result["market_bias"],
        confidence=result["confidence"],
        momentum_status=result["momentum_status"],
        risk_level=result["risk_level"],
        summary=result["summary"],
        key_drivers=result["key_drivers"],
        entry=result["entry"],
        target=result["target"],
        stretch_target=result["stretch_target"],
        stop=result["stop"],
        invalidation_level=result["invalidation_level"],
        expected_move=result["expected_move"],
        expected_duration=result["expected_duration"],
        historical_similarity=result["historical_similarity"],
        risk_notes=result["risk_notes"],
        news_context=context["recent_news"],
        calendar_context={
            "upcoming": context["upcoming_events"],
            "released": context["released_events"],
        },
        timeline=timeline,
        model=result.get("model", "mock-rules-v1"),
        market_snapshot_id=snapshot.id,
    )
    db.add(analysis)
    db.commit()
    db.refresh(analysis)

    payload = serialize_analysis(analysis, market=serialize_market(snapshot))
    payload["context"] = {
        "upcoming_events": context["upcoming_events"],
        "released_events": context["released_events"],
        "recent_news": context["recent_news"],
    }
    return payload


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
