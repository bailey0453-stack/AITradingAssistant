"""Directional signal for USD/MXN.

Scoring (which signals count and by how much) lives in the configurable
``signal_weights`` engine — there are **no hard-coded driver weights here**.
This module turns the weighted score into a tradeable shape: direction,
confidence, price levels, and an explainable breakdown.

Convention: USD/MXN is "pesos per 1 USD" i.e. higher number = stronger USD.
  - BUY_USD  => expect USD/MXN to rise
  - SELL_USD => expect USD/MXN to fall
"""

from __future__ import annotations

from app.services.market_data import MarketData
from app.services.signal_weights import score_signals

# Move sizing as a fraction of current price (trade construction, not signal
# weighting — kept here intentionally).
_TARGET_PCT = 0.005
_STRETCH_PCT = 0.011
_STOP_PCT = 0.004


def _risk_level(market: MarketData) -> str:
    """Coarse risk read from the VIX level (placeholder macro)."""
    vix = market.vix or 0.0
    if vix >= 20:
        return "high"
    if vix >= 16:
        return "elevated"
    return "low"


def _expected_move(price: float, target: float | None, direction: str) -> str:
    if not price or target is None or direction in ("NO_TRADE", "HOLD"):
        return "flat / range-bound"
    pct = (target / price - 1.0) * 100.0
    return f"{pct:+.2f}% (spot {price} -> {target})"


def compute_signal(
    market: MarketData,
    news: list[dict] | None = None,
    released_events: list[dict] | None = None,
    momentum: dict | None = None,
    weights: dict | None = None,
) -> dict:
    """Score the market via the weighting engine and attach trade levels."""
    scored = score_signals(
        market,
        news=news,
        released_events=released_events,
        momentum=momentum,
        weights=weights,
    )
    direction = scored["direction"]
    price = market.usdmxn or 0.0

    if direction == "BUY_USD":
        target = round(price * (1 + _TARGET_PCT), 4)
        stretch = round(price * (1 + _STRETCH_PCT), 4)
        stop = round(price * (1 - _STOP_PCT), 4)
    elif direction == "SELL_USD":
        target = round(price * (1 - _TARGET_PCT), 4)
        stretch = round(price * (1 - _STRETCH_PCT), 4)
        stop = round(price * (1 + _STOP_PCT), 4)
    else:
        target = stretch = stop = None

    return {
        "direction": direction,
        "confidence": scored["confidence"],
        "trade_score": scored["trade_score"],
        "is_actionable": scored["is_actionable"],
        "market_bias": scored["market_bias"],
        "risk_level": _risk_level(market),
        "score": scored["net_score"],
        "momentum_status": scored["momentum_status"],
        "key_drivers": scored["key_drivers"],
        "entry": round(price, 4) if price else None,
        "target": target,
        "stretch_target": stretch,
        "stop": stop,
        "invalidation_level": stop,
        "expected_move": _expected_move(price, target, direction),
        # Weighted-engine breakdown (for debugging / dashboard).
        "weighted_contributions": scored["weighted_contributions"],
        "conflicting_signals": scored["conflicting_signals"],
        "signal_breakdown": {
            "usd_score": scored["usd_score"],
            "mxn_score": scored["mxn_score"],
            "net_score": scored["net_score"],
            "total_score": scored["total_score"],
            "trade_threshold": scored["trade_threshold"],
            "action_threshold": scored.get("action_threshold"),
            "direction_epsilon": scored.get("direction_epsilon"),
            "weights_version": scored["weights_version"],
            "weights": scored["weights"],
        },
    }
