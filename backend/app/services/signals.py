"""Pure signal heuristics for USD/MXN.

Turns market drivers (+ optional news) into a directional bias, confidence, and
price levels. This is intentionally simple and deterministic so it is easy to
test and reason about; a model-based analyzer can build on top of it.

Convention: USD/MXN is "USD per 1 MXN-quote" i.e. higher number = stronger USD.
  - BUY_USD  => expect USD/MXN to rise
  - SELL_USD => expect USD/MXN to fall
"""

from __future__ import annotations

from app.services.market_data import MarketData

# Weights for each driver's contribution to the USD-strength score.
_W_DXY = 1.2
_W_YIELD = 1.0
_W_OIL = 0.8  # higher oil => risk-on / stronger MXN => pushes score down
_W_NEWS = 0.6

# Score magnitude above which we take a trade (otherwise NO_TRADE).
_TRADE_THRESHOLD = 0.35

# Move sizing as fraction of current price.
_TARGET_PCT = 0.005
_STRETCH_PCT = 0.011
_STOP_PCT = 0.004


def _news_bias(news: list[dict] | None) -> tuple[float, list[str]]:
    """Net USD bias from news headlines, weighted by importance."""
    if not news:
        return 0.0, []

    impact_weight = {"high": 1.0, "medium": 0.6, "low": 0.3}
    score = 0.0
    drivers: list[str] = []
    for item in news:
        importance = str(item.get("importance", item.get("impact", "low"))).lower()
        weight = impact_weight.get(importance, 0.3)
        sentiment = str(item.get("sentiment", "neutral")).lower()
        if sentiment == "usd_bullish":
            score += weight
            drivers.append(f"News (USD+): {item.get('headline', '')}")
        elif sentiment == "mxn_bullish":
            score -= weight
            drivers.append(f"News (MXN+): {item.get('headline', '')}")
    return score, drivers


def _risk_level(market: MarketData) -> str:
    """Coarse risk read from the VIX level (placeholder macro)."""
    vix = market.vix or 0.0
    if vix >= 20:
        return "high"
    if vix >= 16:
        return "elevated"
    return "low"


def _expected_move(price: float, target: float | None, direction: str) -> str:
    if not price or target is None or direction == "NO_TRADE":
        return "flat / range-bound"
    pct = (target / price - 1.0) * 100.0
    return f"{pct:+.2f}% (spot {price} -> {target})"


def compute_signal(market: MarketData, news: list[dict] | None = None) -> dict:
    drivers_meta = market.drivers or {}
    dxy_delta = float(drivers_meta.get("dxy_delta", 0.0))
    yield_delta = float(drivers_meta.get("yield_delta", 0.0))
    oil_delta = float(drivers_meta.get("oil_delta", 0.0))

    news_score, news_drivers = _news_bias(news)

    # Normalize deltas to comparable ranges before weighting.
    score = (
        _W_DXY * (dxy_delta / 0.6)
        + _W_YIELD * (yield_delta / 0.08)
        - _W_OIL * (oil_delta / 2.5)
        + _W_NEWS * news_score
    )

    key_drivers: list[str] = []
    if abs(dxy_delta) > 0.01:
        key_drivers.append(
            f"DXY {'firmer' if dxy_delta > 0 else 'softer'} ({market.dxy})"
        )
    if abs(yield_delta) > 0.005:
        key_drivers.append(
            f"US 10Y yield {'up' if yield_delta > 0 else 'down'} ({market.treasury_yield}%)"
        )
    if abs(oil_delta) > 0.05:
        key_drivers.append(
            f"Oil {'up' if oil_delta > 0 else 'down'} ({market.oil}) "
            f"=> {'MXN support' if oil_delta > 0 else 'MXN drag'}"
        )
    key_drivers.extend(news_drivers)

    price = market.usdmxn or 0.0

    if score >= _TRADE_THRESHOLD:
        direction = "BUY_USD"
        momentum = "Bullish USD"
        market_bias = "USD bullish"
        target = round(price * (1 + _TARGET_PCT), 4)
        stretch = round(price * (1 + _STRETCH_PCT), 4)
        stop = round(price * (1 - _STOP_PCT), 4)
    elif score <= -_TRADE_THRESHOLD:
        direction = "SELL_USD"
        momentum = "Bearish USD"
        market_bias = "USD bearish"
        target = round(price * (1 - _TARGET_PCT), 4)
        stretch = round(price * (1 - _STRETCH_PCT), 4)
        stop = round(price * (1 + _STOP_PCT), 4)
    else:
        direction = "NO_TRADE"
        momentum = "Neutral / range-bound"
        market_bias = "Neutral"
        target = None
        stretch = None
        stop = None

    # Confidence: scale |score| into 0..100 with a soft cap.
    confidence = round(min(95.0, abs(score) * 28.0), 1)
    if direction == "NO_TRADE":
        confidence = round(min(confidence, 35.0), 1)

    # Trade score: a 0..100 conviction read distinct from confidence.
    trade_score = round(min(100.0, abs(score) * 30.0), 1)

    if not key_drivers:
        key_drivers = ["No dominant driver; mixed/flat inputs"]

    return {
        "direction": direction,
        "confidence": confidence,
        "trade_score": trade_score,
        "market_bias": market_bias,
        "risk_level": _risk_level(market),
        "score": round(score, 4),
        "momentum_status": momentum,
        "key_drivers": key_drivers,
        "entry": round(price, 4) if price else None,
        "target": target,
        "stretch_target": stretch,
        "stop": stop,
        "invalidation_level": stop,
        "expected_move": _expected_move(price, target, direction),
    }
