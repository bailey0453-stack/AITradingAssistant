"""USD/MXN AI analysis endpoints."""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, Query
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.database import get_db
from app.models import AnalysisSnapshot
from app.routers.market import capture_market_snapshot, serialize_market
from app.services.ai_analysis import get_analyzer
from app.services.context_builder import build_context, build_timeline
from app.services.history import (
    aggregate_statistics,
    blend_confidence,
    find_similar,
    persist_matches,
    probability_forecast,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/analysis", tags=["analysis"])


def _historical_intelligence(db: Session, market, context: dict, result: dict) -> dict:
    """Compute Phase 4 history block + blended confidence (resilient).

    Mutates ``result`` in place (confidence, historical_similarity, historical,
    probabilities, confidence_breakdown) and returns the rankings/match list so
    the caller can persist similarity matches once the snapshot id exists.
    """
    out = {"matches": [], "query_vector": None, "historical_context": None,
           "probabilities": None, "confidence_breakdown": None}
    try:
        context["market_regime"] = result.get("market_regime")
        hist = find_similar(db, context, regime=result.get("market_regime"), top_n=8)
        matches = hist["top_matches"]
        out["matches"] = matches
        out["query_vector"] = hist["query_vector"]

        current_price = market.usdmxn
        stats = aggregate_statistics(
            matches, direction=result["direction"], current_price=current_price
        )

        target_1 = result.get("target")
        stretch = result.get("stretch_target")
        target_2 = (
            round((target_1 + stretch) / 2.0, 4)
            if target_1 is not None and stretch is not None
            else None
        )
        targets = {
            "target_1": target_1,
            "target_2": target_2,
            "stretch": stretch,
            "stop": result.get("stop"),
        }
        probabilities = probability_forecast(
            matches, current_price, result["direction"], targets
        )

        # Blended, configurable confidence.
        vix = market.vix if market.vix is not None else 15.0
        vol_quality = max(0.0, min(100.0, 100.0 - max(0.0, (vix - 12.0)) * 5.0))
        n_news = len(context.get("recent_news") or [])
        n_events = len(context.get("upcoming_events") or []) + len(
            context.get("released_last_24h") or []
        )
        data_quality = max(0.0, min(100.0, n_news * 8.0 + n_events * 6.0))
        components = {
            "signal": result.get("confidence"),
            "historical": round(hist["best_similarity"] * 100.0, 1) if matches else None,
            "regime": (result.get("market_regime") or {}).get("confidence"),
            "volatility": round(vol_quality, 1),
            "data_quality": round(data_quality, 1),
        }
        confidence_breakdown = blend_confidence(components)

        keep = (
            "event_type", "event_name", "release_time", "similarity_score",
            "windows", "max_favorable_excursion", "max_adverse_excursion",
            "time_to_peak_hours", "reversal_behavior",
        )
        historical_context = {
            "best_similarity": hist["best_similarity"],
            "considered": hist["considered"],
            "sample_size": stats.get("sample_size"),
            "statistics": stats,
            "top_matches": [{k: m.get(k) for k in keep} for m in matches[:5]],
        }

        if confidence_breakdown.get("value") is not None:
            result["confidence"] = confidence_breakdown["value"]
        result["historical_similarity"] = {
            "status": "active",
            "best_similarity": hist["best_similarity"],
            "sample_size": stats.get("sample_size"),
            "win_rate": stats.get("win_rate"),
            "average_move": stats.get("average_move"),
            "median_move": stats.get("median_move"),
            "note": "Ranked vs backfilled sample history; tune via SIMILARITY_WEIGHTS.",
        }
        result["historical"] = historical_context
        result["probabilities"] = probabilities
        result["confidence_breakdown"] = confidence_breakdown

        out["historical_context"] = historical_context
        out["probabilities"] = probabilities
        out["confidence_breakdown"] = confidence_breakdown
    except Exception:  # noqa: BLE001
        logger.exception("History block failed; using signal-only confidence.")
    return out


def serialize_analysis(row: AnalysisSnapshot, market: dict | None = None) -> dict:
    sb = row.signal_breakdown or {}
    return {
        "id": row.id,
        "pair": row.pair,
        "created_at": row.created_at.isoformat() if row.created_at else None,
        "direction": row.direction,
        "trade_score": row.trade_score,
        "market_bias": row.market_bias,
        "confidence": row.confidence,
        # Top-level convenience copies of the weighted scores (also in signal_breakdown).
        "usd_score": sb.get("usd_score"),
        "mxn_score": sb.get("mxn_score"),
        "net_bias": sb.get("net_score"),
        "momentum_status": row.momentum_status,
        "historical_similarity": row.historical_similarity,
        "risk_level": row.risk_level,
        "summary": row.summary,
        "key_drivers": row.key_drivers,
        "market_drivers": row.market_drivers,
        "bullish_factors": row.bullish_factors,
        "bearish_factors": row.bearish_factors,
        "upcoming_risks": row.upcoming_risks,
        "weighted_contributions": row.weighted_contributions,
        "conflicting_signals": row.conflicting_signals,
        "signal_breakdown": row.signal_breakdown,
        "what_would_change_my_mind": row.what_would_change_my_mind,
        "market_regime": row.market_regime,
        "opportunity_grade": row.opportunity_grade,
        "opportunity_grade_detail": row.opportunity_grade_detail,
        "historical": row.historical_context,
        "probabilities": row.probabilities,
        "confidence_breakdown": row.confidence_breakdown,
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
        context=context,
    )

    # Phase 4: historical comparison + blended confidence (mutates result).
    history = _historical_intelligence(db, market, context, result)

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
        market_drivers=result["market_drivers"],
        bullish_factors=result["bullish_factors"],
        bearish_factors=result["bearish_factors"],
        upcoming_risks=result["upcoming_risks"],
        weighted_contributions=result["weighted_contributions"],
        conflicting_signals=result["conflicting_signals"],
        signal_breakdown=result["signal_breakdown"],
        market_regime=result["market_regime"],
        opportunity_grade=result["opportunity_grade"],
        opportunity_grade_detail=result["opportunity_grade_detail"],
        what_would_change_my_mind=result["what_would_change_my_mind"],
        historical_context=history.get("historical_context"),
        probabilities=history.get("probabilities"),
        confidence_breakdown=history.get("confidence_breakdown"),
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
            "released_last_24h": context.get("released_last_24h", []),
            "source": context.get("calendar_source", "mock"),
        },
        timeline=timeline,
        model=result.get("model", "mock-rules-v1"),
        market_snapshot_id=snapshot.id,
    )
    db.add(analysis)
    db.commit()
    db.refresh(analysis)

    # Persist the similarity matches now that we have the recommendation id
    # (keeps the proprietary link in similarity_matches, not the public tables).
    if history.get("matches"):
        persist_matches(
            db, history.get("query_vector") or {}, history["matches"], analysis.id
        )

    payload = serialize_analysis(analysis, market=serialize_market(snapshot))
    payload["context"] = {
        "upcoming_events": context["upcoming_events"],
        "released_events": context["released_events"],
        "released_last_24h": context.get("released_last_24h", []),
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
