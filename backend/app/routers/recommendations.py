"""Paper AI recommendation tracking endpoints.

These are model signals (paper recommendations), separate from any real trade.
- GET  /recommendations/recent       — latest stored recommendations
- GET  /recommendations/performance  — scored-outcome performance summary (fast)
- POST /recommendations/evaluate     — score due recommendations (bounded)
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.database import get_db
from app.models import AnalysisSnapshot, MarketSnapshot, Recommendation
from app.services.recommendation_evaluator import evaluate_due, performance_summary
from app.versions import version_tags

router = APIRouter(prefix="/recommendations", tags=["recommendations"])


def _news_category(market_snapshot: MarketSnapshot) -> str | None:
    """Most common tag across the snapshot's news, as a coarse category."""
    news = getattr(market_snapshot, "news", None) or []
    counts: dict[str, int] = {}
    for item in news:
        for tag in (item or {}).get("tags") or []:
            counts[tag] = counts.get(tag, 0) + 1
    return max(counts, key=counts.get) if counts else None


def store_recommendation(
    db: Session, analysis: AnalysisSnapshot, market_snapshot: MarketSnapshot
) -> Recommendation:
    """Persist a versioned, indexed paper recommendation from an analysis snapshot."""
    regime = (analysis.market_regime or {}).get("primary") if analysis.market_regime else None
    vols = version_tags()
    reco = Recommendation(
        **vols,
        pair=analysis.pair,
        spot_price=market_snapshot.usdmxn if market_snapshot else None,
        direction=analysis.direction,
        confidence=analysis.confidence,
        opportunity_grade=analysis.opportunity_grade,
        trade_score=analysis.trade_score,
        market_regime=analysis.market_regime,
        regime=regime,
        volatility=market_snapshot.vix if market_snapshot else None,
        news_category=_news_category(market_snapshot),
        target=analysis.target,
        stretch_target=analysis.stretch_target,
        stop=analysis.stop,
        time_horizons=analysis.time_horizons,
        primary_trade_plan={
            "entry": analysis.entry,
            "target": analysis.target,
            "stretch_target": analysis.stretch_target,
            "stop": analysis.stop,
            "expected_move": analysis.expected_move,
            "expected_duration": analysis.expected_duration,
        },
        key_drivers=analysis.key_drivers,
        bullish_factors=analysis.bullish_factors,
        bearish_factors=analysis.bearish_factors,
        conflicting_signals=analysis.conflicting_signals,
        historical_similarity=analysis.historical_similarity,
        strategist=analysis.strategist,
        analysis_snapshot_id=analysis.id,
        market_snapshot_id=market_snapshot.id if market_snapshot else None,
    )
    db.add(reco)
    db.commit()
    db.refresh(reco)
    return reco


def serialize_recommendation(reco: Recommendation, with_outcomes: bool = False) -> dict:
    data = {
        "id": reco.id,
        "recommendation_uuid": reco.recommendation_uuid,
        "created_at": reco.created_at.isoformat() if reco.created_at else None,
        "model_version": reco.model_version,
        "reasoning_engine_version": reco.reasoning_engine_version,
        "weighting_profile": reco.weighting_profile,
        "historical_engine_version": reco.historical_engine_version,
        "pair": reco.pair,
        "spot_price": reco.spot_price,
        "direction": reco.direction,
        "confidence": reco.confidence,
        "opportunity_grade": reco.opportunity_grade,
        "trade_score": reco.trade_score,
        "market_regime": reco.market_regime,
        "regime": reco.regime,
        "volatility": reco.volatility,
        "news_category": reco.news_category,
        "target": reco.target,
        "stretch_target": reco.stretch_target,
        "stop": reco.stop,
        "time_horizons": reco.time_horizons,
        "primary_trade_plan": reco.primary_trade_plan,
        "key_drivers": reco.key_drivers,
        "bullish_factors": reco.bullish_factors,
        "bearish_factors": reco.bearish_factors,
        "conflicting_signals": reco.conflicting_signals,
        "historical_similarity": reco.historical_similarity,
        "strategist": reco.strategist,
        "evaluation_status": reco.evaluation_status,
        "last_evaluated_at": reco.last_evaluated_at.isoformat() if reco.last_evaluated_at else None,
        "analysis_snapshot_id": reco.analysis_snapshot_id,
        "market_snapshot_id": reco.market_snapshot_id,
    }
    if with_outcomes:
        data["outcomes"] = [
            {
                "horizon": o.horizon,
                "evaluated_at": o.evaluated_at.isoformat() if o.evaluated_at else None,
                "spot_at_evaluation": o.spot_at_evaluation,
                "return_pct": o.return_pct,
                "direction_correct": o.direction_correct,
                "target_hit": o.target_hit,
                "stretch_hit": o.stretch_hit,
                "stop_hit": o.stop_hit,
                "max_favorable_excursion": o.max_favorable_excursion,
                "max_adverse_excursion": o.max_adverse_excursion,
                "time_to_target_hours": o.time_to_target_hours,
                "time_to_stop_hours": o.time_to_stop_hours,
                "holding_time_hours": o.holding_time_hours,
                "actionable": o.actionable,
                "hedge_return_pct": o.hedge_return_pct,
                "gross_pnl_usd": o.gross_pnl_usd,
                "net_pnl_usd": o.net_pnl_usd,
            }
            for o in sorted(reco.outcomes, key=lambda x: x.horizon)
        ]
    return data


@router.get("/recent")
def recent_recommendations(
    limit: int = Query(default=20, ge=1, le=200),
    with_outcomes: bool = Query(default=True),
    db: Session = Depends(get_db),
) -> dict:
    rows = db.execute(
        select(Recommendation).order_by(Recommendation.created_at.desc()).limit(limit)
    ).scalars().all()
    return {
        "count": len(rows),
        "recommendations": [serialize_recommendation(r, with_outcomes) for r in rows],
    }


@router.get("/performance")
def performance(db: Session = Depends(get_db)) -> dict:
    """Aggregated performance over already-scored outcomes (fast read)."""
    return performance_summary(db)


@router.post("/evaluate")
def evaluate(
    limit: int = Query(default=200, ge=1, le=2000),
    db: Session = Depends(get_db),
) -> dict:
    """Score due recommendations (bounded). Intended for manual/scheduled use."""
    return evaluate_due(db, limit=limit)
