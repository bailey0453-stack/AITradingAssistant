"""Historical intelligence endpoints (Phase 4).

  GET /history/events        — backfilled historical events (filterable).
  GET /history/similar       — events most like the current market context.
  GET /history/statistics    — aggregate stats over the similar events.
  GET /history/probabilities — probability of reaching target/stop levels.

All endpoints ensure the mock/sample history is seeded first, so they return
valid data with no paid providers and never depend on prior calls.
"""

from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.database import get_db
from app.services.context_builder import build_context
from app.services.history import (
    aggregate_statistics,
    find_similar,
    list_events,
    probability_forecast,
)
from app.services.history.historical_events import ensure_history_seeded
from app.services.market_data import get_market_data
from app.services.signals import compute_signal

router = APIRouter(prefix="/history", tags=["history"])


def _live_context_and_signal(db: Session) -> tuple[dict, dict]:
    """Build the current context + signal (mock-safe, read-only market fetch)."""
    market = get_market_data()
    context = build_context(db, market)
    signal = compute_signal(
        market,
        news=context["recent_news"],
        released_events=context.get("released_last_24h"),
        momentum=context.get("momentum"),
    )
    # Attach the regime so similarity can match on it.
    regime = None
    try:
        from app.services.market_regime import detect_regime

        regime = detect_regime(
            market,
            news=context["recent_news"],
            calendar=context["upcoming_events"] + context["released_events"],
            momentum=context.get("momentum"),
            signal=signal,
        )
    except Exception:  # noqa: BLE001
        regime = None
    context["market_regime"] = regime
    signal["_regime"] = regime
    return context, signal


@router.get("/events")
def history_events(
    event_type: Optional[str] = Query(default=None),
    limit: int = Query(default=100, ge=1, le=500),
    db: Session = Depends(get_db),
) -> dict:
    ensure_history_seeded(db)
    events = list_events(db, event_type=event_type, limit=limit)
    return {"count": len(events), "event_type": event_type, "events": events}


@router.get("/similar")
def history_similar(
    top_n: int = Query(default=5, ge=1, le=25),
    db: Session = Depends(get_db),
) -> dict:
    context, signal = _live_context_and_signal(db)
    hist = find_similar(db, context, regime=signal.get("_regime"), top_n=top_n)
    return {
        "query_vector": hist["query_vector"],
        "considered": hist["considered"],
        "best_similarity": hist["best_similarity"],
        "top_matches": hist["top_matches"],
    }


@router.get("/statistics")
def history_statistics(
    top_n: int = Query(default=10, ge=1, le=50),
    db: Session = Depends(get_db),
) -> dict:
    context, signal = _live_context_and_signal(db)
    hist = find_similar(db, context, regime=signal.get("_regime"), top_n=top_n)
    current_price = (context.get("market") or {}).get("usdmxn")
    stats = aggregate_statistics(
        hist["top_matches"], direction=signal["direction"], current_price=current_price
    )
    return {
        "direction": signal["direction"],
        "current_price": current_price,
        "best_similarity": hist["best_similarity"],
        "statistics": stats,
    }


@router.get("/probabilities")
def history_probabilities(
    top_n: int = Query(default=10, ge=1, le=50),
    db: Session = Depends(get_db),
) -> dict:
    context, signal = _live_context_and_signal(db)
    hist = find_similar(db, context, regime=signal.get("_regime"), top_n=top_n)
    current_price = (context.get("market") or {}).get("usdmxn")
    targets = {
        "target_1": signal.get("target"),
        "target_2": None,
        "stretch": signal.get("stretch_target"),
        "stop": signal.get("stop"),
    }
    probs = probability_forecast(
        hist["top_matches"], current_price, signal["direction"], targets
    )
    return {
        "direction": signal["direction"],
        "current_price": current_price,
        "probabilities": probs,
    }
