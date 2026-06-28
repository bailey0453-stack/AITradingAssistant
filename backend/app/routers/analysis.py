"""USD/MXN AI analysis endpoints."""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, Query
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.database import get_db
from app.models import AnalysisSnapshot
from app.routers.market import capture_market_snapshot, serialize_market
from app.routers.recommendations import store_recommendation
from app.services import cache_manager
from app.services.ai_analysis import get_analyzer
from app.services.context_builder import build_context, build_timeline
from app.services.history import (
    aggregate_statistics,
    blend_confidence,
    evidence_narrative,
    find_similar,
    persist_matches,
    probability_forecast,
    setup_percentile,
)
from app.services.history.historical_events import load_reactions

# Direction sign for "favorable" USD/MXN moves (BUY_USD wants up, SELL_USD down).
_DIR_SIGN = {"BUY_USD": 1.0, "SELL_USD": -1.0, "NO_TRADE": 1.0}


def _rep_move(match: dict) -> float | None:
    """Representative USD/MXN move for a match (1d, falling back to shorter)."""
    w = match.get("windows") or {}
    for key in ("1d", "4h", "1h", "3d", "5d"):
        if w.get(key) is not None:
            return w[key]
    return None


# Map a reaction's provenance to a dashboard-facing label.
_BACKFILLED_QUALITIES = {"imported", "official", "vendor_free", "vendor_paid", "backfilled"}


def _historical_source_label(matches: list[dict]) -> str:
    """Summarize the provenance of the matched history (sample/backfilled/live)."""
    qualities = {str(m.get("source_quality") or "").lower() for m in matches or []}
    if "live" in qualities:
        return "live"
    if qualities & _BACKFILLED_QUALITIES:
        return "backfilled"
    return "sample"

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
        # Phase 5: nearest-neighbor matching over a broad evidence base (top 25).
        hist = find_similar(db, context, regime=result.get("market_regime"), top_n=25)
        matches = hist["top_matches"]
        out["matches"] = matches
        out["query_vector"] = hist["query_vector"]

        direction = result["direction"]
        current_price = market.usdmxn
        stats = aggregate_statistics(
            matches, direction=direction, current_price=current_price
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
            matches, current_price, direction, targets
        )

        # Setup strength percentile: rank today's expected directional move vs
        # the full historical library of directional moves.
        percentile = None
        try:
            if direction in ("BUY_USD", "SELL_USD") and stats.get("average_move") is not None:
                sign = _DIR_SIGN.get(direction, 1.0)
                reference = [
                    _rep_move(r) * sign
                    for r in load_reactions(db)
                    if _rep_move(r) is not None
                ]
                percentile = setup_percentile(reference, stats["average_move"] * sign)
        except Exception:  # noqa: BLE001
            logger.exception("Setup percentile failed; continuing.")

        # Evidence-based historical brief sentence(s).
        evidence_summary = evidence_narrative(stats, direction, percentile)

        # Blended, configurable confidence. The six conceptual inputs (signals,
        # historical evidence, regime, volatility, news quality, calendar
        # certainty) are surfaced; news + calendar richness feed data_quality.
        vix = market.vix if market.vix is not None else 15.0
        vol_quality = max(0.0, min(100.0, 100.0 - max(0.0, (vix - 12.0)) * 5.0))
        n_news = len(context.get("recent_news") or [])
        n_events = len(context.get("upcoming_events") or []) + len(
            context.get("released_last_24h") or []
        )
        news_quality = round(max(0.0, min(100.0, n_news * 12.0)), 1)
        calendar_certainty = round(max(0.0, min(100.0, n_events * 12.0)), 1)
        data_quality = max(0.0, min(100.0, n_news * 8.0 + n_events * 6.0))
        components = {
            "signal": result.get("confidence"),
            "historical": round(hist["best_similarity"] * 100.0, 1) if matches else None,
            "regime": (result.get("market_regime") or {}).get("confidence"),
            "volatility": round(vol_quality, 1),
            "data_quality": round(data_quality, 1),
        }
        confidence_breakdown = blend_confidence(components)
        confidence_breakdown["inputs"] = {
            "current_signals": result.get("confidence"),
            "historical_evidence": components["historical"],
            "market_regime_confidence": components["regime"],
            "volatility_quality": components["volatility"],
            "news_quality": news_quality,
            "calendar_certainty": calendar_certainty,
            "data_completeness": components["data_quality"],
        }

        keep = (
            "event_type", "event_name", "release_time", "similarity_score",
            "distance_score", "rank", "windows", "max_favorable_excursion",
            "max_adverse_excursion", "time_to_peak_hours", "reversal_behavior",
        )
        historical_source = _historical_source_label(matches)
        historical_context = {
            "best_similarity": hist["best_similarity"],
            "best_distance": hist.get("best_distance"),
            "considered": hist["considered"],
            "sample_size": stats.get("sample_size"),
            "setup_percentile": percentile,
            "evidence_summary": evidence_summary,
            "historical_source": historical_source,
            "statistics": stats,
            "top_matches": [{k: m.get(k) for k in keep} for m in matches[:25]],
        }
        out["historical_source"] = historical_source

        # Refine the history-dependent "explain every number" entries now that
        # the evidence base + blended confidence are computed.
        explanations = dict(result.get("explanations") or {})
        if confidence_breakdown.get("formula"):
            explanations["confidence"] = (
                "Blended confidence — " + confidence_breakdown["formula"]
                + " Components: " + "; ".join(confidence_breakdown.get("explanation") or [])
            )
        explanations["historical_similarity"] = (
            f"Best analog similarity {round(hist['best_similarity'] * 100, 1)}% "
            f"(distance {hist.get('best_distance')}) across {hist['considered']} "
            f"historical events; weighted blend of regime, event type, DXY, yields, "
            f"oil, VIX, momentum and news-tag overlap (SIMILARITY_WEIGHTS)."
        )
        if probabilities.get("method"):
            explanations["probability"] = probabilities["method"]
        result["explanations"] = explanations
        result["evidence_summary"] = evidence_summary

        if confidence_breakdown.get("value") is not None:
            result["confidence"] = confidence_breakdown["value"]
            # Refresh the strategist brief so its confidence matches the blended
            # headline confidence (Phase 4.5 + Phase 4 consistency).
            try:
                from app.services.ai_analysis import RuleBasedAnalyzer

                strat = RuleBasedAnalyzer.strategist_from_result(
                    result, market, confidence_override=confidence_breakdown["value"]
                )
                result["strategist"] = strat
                for k in _STRATEGIST_FIELDS:
                    result[k] = strat[k]
            except Exception:  # noqa: BLE001
                logger.exception("Strategist refresh failed; keeping signal-confidence brief.")
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

        # Refresh the multi-horizon outlook so the 1-2d / >2d horizons fold in
        # historical analogs and reflect the final blended confidence.
        try:
            from app.services.ai_analysis import RuleBasedAnalyzer

            result["time_horizons"] = RuleBasedAnalyzer.time_horizons_from_result(
                result,
                market,
                historical=historical_context,
                confidence_override=confidence_breakdown.get("value"),
            )
        except Exception:  # noqa: BLE001
            logger.exception("Horizon refresh failed; keeping signal-only horizons.")

        out["historical_context"] = historical_context
        out["probabilities"] = probabilities
        out["confidence_breakdown"] = confidence_breakdown
    except Exception:  # noqa: BLE001
        logger.exception("History block failed; using signal-only confidence.")
    return out


_STRATEGIST_FIELDS = (
    "executive_summary",
    "why_this_grade",
    "why_not_higher",
    "why_not_lower",
    "current_trade_view",
    "trader_action",
    "quote_guidance",
    "risk_watchlist",
    "invalidation_triggers",
)


def serialize_analysis(row: AnalysisSnapshot, market: dict | None = None) -> dict:
    sb = row.signal_breakdown or {}
    strat = row.strategist or {}
    payload = {
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
        "strategist": row.strategist,
        "historical": row.historical_context,
        "probabilities": row.probabilities,
        "confidence_breakdown": row.confidence_breakdown,
        "explanations": row.explanations,
        "evidence_summary": row.evidence_summary,
        "entry": row.entry,
        "target": row.target,
        "stretch_target": row.stretch_target,
        "stop": row.stop,
        "expected_move": row.expected_move,
        "expected_duration": row.expected_duration,
        "time_horizons": row.time_horizons,
        "invalidation_level": row.invalidation_level,
        "risk_notes": row.risk_notes,
        "timeline": row.timeline,
        "model": row.model,
        "market_snapshot_id": row.market_snapshot_id,
        "market": market,
    }
    # Spread the strategist narrative to top-level fields for easy consumption.
    for key in _STRATEGIST_FIELDS:
        payload[key] = strat.get(key)
    return payload


def _unavailable_analysis_payload(market, market_meta: dict, news_source: str) -> dict:
    """Safe no-trade response when no usable current market quote exists.

    Never invents a price, target, stretch, stop, or actionable recommendation.
    The dashboard renders an explicit "Market data unavailable" warning.
    """
    warning = market_meta.get("warning") or (
        "Live market data unavailable and no recent cached real quote exists."
    )
    payload = {
        "pair": "USDMXN",
        "market_data_unavailable": True,
        "direction": "NO_TRADE",
        "trade_score": None,
        "market_bias": "NEUTRAL",
        "confidence": None,
        "momentum_status": None,
        "risk_level": None,
        "summary": warning,
        "key_drivers": [],
        "market_drivers": [],
        "bullish_factors": [],
        "bearish_factors": [],
        "upcoming_risks": [],
        "historical_similarity": {"status": "unavailable", "note": warning},
        # No actionable trade plan without a real current price.
        "entry": None, "target": None, "stretch_target": None, "stop": None,
        "expected_move": None, "expected_duration": None,
        "invalidation_level": None, "time_horizons": [],
        "opportunity_grade": "PASS",
        "strategist": None,
        "trader_action": (
            "Do not initiate a trade. Market data is unavailable; wait for a "
            "live quote before acting."
        ),
        "market": {
            "pair": "USDMXN", "usdmxn": None, "inverse_usdmxn": None,
            "dxy": None, "us2y": None, "us10y": None, "treasury_yield": None,
            "oil": None, "gold": None, "sp_futures": None, "vix": None,
            "provider": "unavailable", "source": "unavailable",
            "sources": getattr(market, "field_sources", {}) or {},
            **market_meta,
        },
        "market_state": {
            k: market_meta.get(k)
            for k in (
                "market_status", "market_reason", "is_open", "cached", "is_stale",
                "fetched_at", "age_minutes", "next_refresh", "last_market_close",
                "next_market_open", "refresh_interval_minutes",
                "market_data_unavailable", "last_real_quote_at",
            )
        },
        "market_status_note": warning,
        "warning": warning,
        "provider_health": cache_manager.health_snapshot(),
        "data_sources": {
            "market": "unavailable",
            "news": news_source,
            "calendar": "unavailable",
            "historical": "unavailable",
        },
        # Explicit, conservative decision overlay: never tradeable.
        "decision_quality": {
            "should_trade_now": False,
            "decision": "WAIT",
            "decision_label": "WAIT",
            "trade_quality_label": "WAIT",
            "trade_quality_score": None,
            "reason": warning,
            "reason_to_wait": warning,
            "better_entry_conditions": [
                "Wait for a live USD/MXN quote (or a recent cached real quote).",
            ],
            "what_to_watch_next": ["Live FX provider connectivity"],
        },
    }
    return payload


@router.get("/usdmxn")
def analyze_usdmxn(db: Session = Depends(get_db)) -> dict:
    """Capture market + news + calendar context, analyze, store, and return."""
    snapshot, market, news, news_source, market_meta = capture_market_snapshot(db)

    # Stale-fallback safety: with no usable current quote, return a safe
    # no-trade response and never persist an actionable recommendation.
    if market_meta.get("market_data_unavailable") or snapshot is None:
        return _unavailable_analysis_payload(market, market_meta, news_source)

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
        strategist=result.get("strategist"),
        explanations=result.get("explanations"),
        evidence_summary=result.get("evidence_summary"),
        entry=result["entry"],
        target=result["target"],
        stretch_target=result["stretch_target"],
        stop=result["stop"],
        invalidation_level=result["invalidation_level"],
        expected_move=result["expected_move"],
        expected_duration=result["expected_duration"],
        time_horizons=result.get("time_horizons"),
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

    # Store a lean, indexed paper recommendation for later outcome evaluation
    # (kept separate from any real trade). Never fail the request over this.
    try:
        store_recommendation(db, analysis, snapshot)
    except Exception:  # noqa: BLE001
        logger.exception("Failed to store paper recommendation; continuing.")
        db.rollback()

    payload = serialize_analysis(
        analysis, market={**serialize_market(snapshot), **market_meta}
    )
    # Market-state awareness: prices may be from the latest session when closed,
    # but news / calendar / historical / regime / strategist are still evaluated.
    payload["market_state"] = {
        k: market_meta.get(k)
        for k in (
            "market_status", "market_reason", "is_open", "cached", "is_stale",
            "fetched_at", "age_minutes", "next_refresh", "last_market_close",
            "next_market_open", "refresh_interval_minutes",
        )
    }
    if market_meta.get("is_open"):
        payload["market_status_note"] = (
            "FX market is open; USD/MXN is live within the refresh interval."
        )
    else:
        payload["market_status_note"] = (
            f"FX market is closed ({market_meta.get('market_reason', '')}). "
            "Prices shown are the latest available session and are not currently "
            "moving; news, calendar, historical evidence, market regime, and the "
            "strategist view are still evaluated."
        )
    payload["provider_health"] = cache_manager.health_snapshot()
    payload["context"] = {
        "upcoming_events": context["upcoming_events"],
        "released_events": context["released_events"],
        "released_last_24h": context.get("released_last_24h", []),
        "recent_news": context["recent_news"],
    }
    # Clearly label every data source so the dashboard never implies sample/mock
    # data is fully real. Values: live | mock | fallback | imported | sample |
    # backfilled (market uses live | mock | fallback).
    payload["data_sources"] = {
        "market": market.source,
        "news": news_source,
        "calendar": context.get("calendar_source", "mock"),
        "historical": history.get("historical_source", "sample"),
    }

    # Phase 5.3: decide whether this trade is worth taking now vs waiting.
    # Never fail the analysis over the decision-quality overlay.
    try:
        from app.services import decision_quality

        payload["decision_quality"] = decision_quality.assess_recommendation(
            db, decision_quality.rec_from_payload(payload)
        )
    except Exception:  # noqa: BLE001
        logger.exception("Decision-quality assessment failed; continuing.")

    # Phase 5.4: provenance metadata (where each number came from + how
    # trustworthy it is) plus an evidence summary. Never fail analysis over it.
    try:
        from app.services import provenance, research_lab

        progress = research_lab.evaluation_progress(db)
        measured_available = bool(progress.get("recommendations_evaluated"))
        measured_accuracy = None
        if measured_available:
            measured_accuracy = research_lab.research_summary(db).get("overall_accuracy")
        similar = (payload.get("decision_quality") or {}).get("similar_track_record") or {}
        prov = provenance.build(
            payload, market_meta,
            measured_accuracy=measured_accuracy,
            measured_available=measured_available,
            similar_measured=bool(similar.get("enough_history")),
        )
        payload["provenance"] = prov
        payload["evidence_overview"] = provenance.overview(prov)
    except Exception:  # noqa: BLE001
        logger.exception("Provenance build failed; continuing.")

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
