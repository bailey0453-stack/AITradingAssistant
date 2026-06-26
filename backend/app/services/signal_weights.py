"""Configurable market signal weighting engine.

This is the single source of truth for *how much each signal counts*. The
analysis engine never hard-codes weights — it asks this module to score the
current market evidence and returns the weighted contributions for debugging.

Tune the model by editing ``DEFAULT_WEIGHTS`` here, or override at runtime via
the ``SIGNAL_WEIGHTS`` environment variable (a JSON object of
``{"signal_key": weight}``) — no analysis code needs to change.

Scoring overview
----------------
1. Each piece of evidence (macro indicator, news category, released data) is
   turned into a *signed, weighted contribution*: ``weight x strength`` with a
   direction of "USD" (favors USD strength) or "MXN" (favors peso strength).
2. We sum USD-favoring and MXN-favoring contributions separately, take the net,
   and derive Trade Score, Confidence, Market Bias, and Key Drivers from it.
3. Signals that oppose the net bias are surfaced as *conflicting signals*.
"""

from __future__ import annotations

import logging

from app.config import Settings, get_settings
from app.services.market_data import MarketData

logger = logging.getLogger(__name__)

WEIGHTS_VERSION = "v1"

# --- Default weights (0..10). Tune here or via the SIGNAL_WEIGHTS env var. ---
DEFAULT_WEIGHTS: dict[str, float] = {
    "fed_rate_decision": 10,
    "banxico_rate_decision": 10,
    "us_cpi": 9,
    "us_ppi": 8,
    "us_nfp": 9,
    "us_gdp": 8,
    "mexico_cpi": 9,
    "mexico_gdp": 8,
    "treasury_yield": 8,
    "dxy": 8,
    "usdmxn_momentum": 7,
    "oil": 7,
    "gold": 5,
    "sp_futures": 5,
    "vix": 6,
    "trade_tariff_news": 8,
    "political_news": 5,
    "general_financial_news": 4,
    "technical_indicators": 5,
}

SIGNAL_LABELS: dict[str, str] = {
    "fed_rate_decision": "Fed Rate Decision",
    "banxico_rate_decision": "Banxico Rate Decision",
    "us_cpi": "US CPI",
    "us_ppi": "US PPI",
    "us_nfp": "US Nonfarm Payrolls",
    "us_gdp": "US GDP",
    "mexico_cpi": "Mexico CPI",
    "mexico_gdp": "Mexico GDP",
    "treasury_yield": "Treasury Yield (2Y/10Y)",
    "dxy": "DXY",
    "usdmxn_momentum": "USD/MXN Momentum",
    "oil": "Oil",
    "gold": "Gold",
    "sp_futures": "S&P Futures",
    "vix": "VIX",
    "trade_tariff_news": "Trade/Tariff News",
    "political_news": "Political News",
    "general_financial_news": "General Financial News",
    "technical_indicators": "Technical Indicators",
}

# --- Scoring tunables (also adjustable without touching the analysis engine) ---
TRADE_THRESHOLD = 4.0   # |net weighted score| needed to take a directional view
SCORE_SCALE = 3.5       # net score -> 0..100 trade score multiplier
MOMENTUM_NORM = 0.05    # USD/MXN move (abs) that counts as full-strength momentum
_IMPORTANCE_STRENGTH = {"high": 1.0, "medium": 0.6, "low": 0.3}
_MIN_STRENGTH = 0.02    # ignore essentially-flat signals


def get_signal_weights(settings: Settings | None = None) -> dict[str, float]:
    """Return the active weights: defaults merged with any configured override.

    Override source: ``settings.signal_weights`` (populated from the
    ``SIGNAL_WEIGHTS`` env var as JSON). Unknown keys are ignored with a warning
    so a typo can never silently change behavior.
    """
    settings = settings or get_settings()
    weights = dict(DEFAULT_WEIGHTS)
    override = getattr(settings, "signal_weights", None)
    if isinstance(override, dict):
        for key, value in override.items():
            if key not in DEFAULT_WEIGHTS:
                logger.warning("Ignoring unknown signal weight key: %r", key)
                continue
            try:
                weights[key] = float(value)
            except (TypeError, ValueError):
                logger.warning("Ignoring non-numeric weight for %r: %r", key, value)
    return weights


# --------------------------------------------------------------------------- #
# Helpers (kept local to avoid import cycles with the analysis engine).
# --------------------------------------------------------------------------- #
def _to_num(value) -> float | None:
    if value is None:
        return None
    text = str(value).strip().lower().replace(",", "").replace("%", "")
    mult = 1.0
    if text.endswith("k"):
        mult, text = 1e3, text[:-1]
    elif text.endswith("m"):
        mult, text = 1e6, text[:-1]
    elif text.endswith("b"):
        mult, text = 1e9, text[:-1]
    try:
        return float(text) * mult
    except ValueError:
        return None


def event_surprise(event: dict) -> str | None:
    """Lean from actual vs forecast: 'USD+' / 'MXN+' / None if not computable."""
    actual = _to_num(event.get("actual"))
    forecast = _to_num(event.get("forecast"))
    if actual is None or forecast is None or actual == forecast:
        return None
    beat = actual > forecast
    impact = (event.get("currency_impact") or "USD").upper()
    if impact == "USD":
        return "USD+" if beat else "MXN+"
    return "MXN+" if beat else "USD+"


def event_signal_key(event_name: str) -> str | None:
    """Map a calendar event name to a weighted signal key (or None)."""
    n = (event_name or "").lower()
    if "fomc" in n or "fed funds" in n or ("federal" in n and "rate" in n):
        return "fed_rate_decision"
    if "banxico" in n or ("mexico" in n and "rate" in n):
        return "banxico_rate_decision"
    if "ppi" in n or "producer price" in n:
        return "us_ppi"
    if "cpi" in n or "inflation" in n:
        return "mexico_cpi" if "mexico" in n else "us_cpi"
    if "nonfarm" in n or "payroll" in n or "nfp" in n:
        return "us_nfp"
    if "gdp" in n:
        return "mexico_gdp" if "mexico" in n else "us_gdp"
    return None


def news_category(item: dict) -> str:
    """Bucket a news item into a weighted news category."""
    tags = [str(t).lower() for t in (item.get("tags") or [])]
    text = f"{item.get('headline', '')} {item.get('summary', '')}".lower()
    if "trade" in tags or "tariff" in text or "tariff" in tags or "trade war" in text:
        return "trade_tariff_news"
    political = ("president", "election", "congress", "policy", "sanction",
                 "geopolit", "political", "white house")
    if any(w in text for w in political):
        return "political_news"
    return "general_financial_news"


def _driver_text(c: dict) -> str:
    return (
        f"{c['label']}: {c['direction']}+ "
        f"(w{c['weight']:g}x{c['strength']:g}={abs(c['contribution']):g}) — {c['detail']}"
    )


def score_signals(
    market: MarketData,
    news: list[dict] | None = None,
    released_events: list[dict] | None = None,
    momentum: dict | None = None,
    weights: dict[str, float] | None = None,
) -> dict:
    """Score current evidence into a weighted directional view.

    Returns direction/bias/scores plus the full list of weighted contributions
    (sorted strongest-first) and any conflicting signals, for debugging.
    """
    weights = weights or get_signal_weights()
    d = market.drivers or {}
    contributions: list[dict] = []

    def add(key: str, direction: str, strength: float, detail: str) -> None:
        strength = min(1.0, max(0.0, strength))
        if strength <= _MIN_STRENGTH:
            return
        weight = float(weights.get(key, DEFAULT_WEIGHTS.get(key, 0.0)))
        if weight <= 0:
            return
        signed = weight * strength * (1.0 if direction == "USD" else -1.0)
        contributions.append(
            {
                "key": key,
                "label": SIGNAL_LABELS.get(key, key),
                "weight": round(weight, 2),
                "direction": direction,
                "strength": round(strength, 3),
                "contribution": round(signed, 2),
                "detail": detail,
            }
        )

    # --- Macro indicators (deltas vs baseline live in market.drivers) ---
    dxy_delta = float(d.get("dxy_delta", 0.0))
    add("dxy", "USD" if dxy_delta > 0 else "MXN", abs(dxy_delta) / 0.6, f"DXY {market.dxy}")

    ydelta = (float(d.get("yield_delta", 0.0)) + float(d.get("us2y_delta", 0.0))) / 2.0
    add(
        "treasury_yield",
        "USD" if ydelta > 0 else "MXN",
        abs(ydelta) / 0.08,
        f"US 2Y/10Y {market.us2y}/{market.us10y}",
    )

    oil_delta = float(d.get("oil_delta", 0.0))  # higher oil -> MXN tailwind
    add("oil", "MXN" if oil_delta > 0 else "USD", abs(oil_delta) / 2.5, f"Oil {market.oil}")

    gold_delta = float(d.get("gold_delta", 0.0))  # higher gold -> mild USD-negative
    add("gold", "MXN" if gold_delta > 0 else "USD", abs(gold_delta) / 25.0, f"Gold {market.gold}")

    sp_delta = float(d.get("sp_delta", 0.0))  # risk-on equities -> MXN
    add("sp_futures", "MXN" if sp_delta > 0 else "USD", abs(sp_delta) / 40.0, f"S&P fut {market.sp_futures}")

    vix_delta = float(d.get("vix_delta", 0.0))  # risk-off -> USD haven
    add("vix", "USD" if vix_delta > 0 else "MXN", abs(vix_delta) / 3.0, f"VIX {market.vix}")

    # --- USD/MXN momentum (from real consecutive snapshots when available) ---
    if momentum and momentum.get("change") is not None:
        change = float(momentum["change"])
        add(
            "usdmxn_momentum",
            "USD" if change > 0 else "MXN",
            abs(change) / MOMENTUM_NORM,
            f"USD/MXN {momentum.get('from')} -> {momentum.get('to')}",
        )

    # --- News categories (aggregate signed strength per category) ---
    news_acc: dict[str, list] = {}
    for item in news or []:
        sentiment = str(item.get("sentiment", "neutral")).lower()
        if sentiment not in ("usd_bullish", "mxn_bullish"):
            continue
        cat = news_category(item)
        strength = _IMPORTANCE_STRENGTH.get(str(item.get("importance", "low")).lower(), 0.3)
        sign = 1.0 if sentiment == "usd_bullish" else -1.0
        acc = news_acc.setdefault(cat, [0.0, item.get("headline", "")])
        acc[0] += sign * strength
    for cat, (signed, headline) in news_acc.items():
        add(cat, "USD" if signed > 0 else "MXN", abs(signed), f"News: {headline}")

    # --- Recently released economic data (surprise vs forecast) ---
    cal_acc: dict[str, list] = {}
    for ev in released_events or []:
        surprise = event_surprise(ev)
        if surprise is None:
            continue
        key = event_signal_key(ev.get("event", ""))
        if not key:
            continue
        strength = _IMPORTANCE_STRENGTH.get(str(ev.get("importance", "low")).lower(), 0.3)
        sign = 1.0 if surprise == "USD+" else -1.0
        acc = cal_acc.setdefault(key, [0.0, ev.get("event", "")])
        acc[0] += sign * strength
    for key, (signed, name) in cal_acc.items():
        add(key, "USD" if signed > 0 else "MXN", abs(signed), f"Data: {name}")

    # --- Aggregate ---
    usd_score = round(sum(c["weight"] * c["strength"] for c in contributions if c["direction"] == "USD"), 2)
    mxn_score = round(sum(c["weight"] * c["strength"] for c in contributions if c["direction"] == "MXN"), 2)
    net = round(usd_score - mxn_score, 2)
    total = round(usd_score + mxn_score, 2)

    if net >= TRADE_THRESHOLD:
        direction, bias, momentum_status = "BUY_USD", "USD bullish", "Bullish USD"
    elif net <= -TRADE_THRESHOLD:
        direction, bias, momentum_status = "SELL_USD", "USD bearish", "Bearish USD"
    else:
        direction, bias, momentum_status = "NO_TRADE", "Neutral", "Neutral / range-bound"

    trade_score = round(min(100.0, abs(net) * SCORE_SCALE), 1)
    confidence = round(min(95.0, (abs(net) / total * 100.0) if total else 0.0), 1)
    if direction == "NO_TRADE":
        confidence = round(min(confidence, 35.0), 1)

    ranked = sorted(contributions, key=lambda c: abs(c["contribution"]), reverse=True)
    key_drivers = [_driver_text(c) for c in ranked[:5]] or [
        "No dominant weighted signal; inputs flat/mixed"
    ]

    # Conflicts: signals opposing the net bias (or, when flat, the strongest of
    # each side that prevents a call).
    if direction == "BUY_USD":
        conflicts = [c for c in ranked if c["direction"] == "MXN"]
    elif direction == "SELL_USD":
        conflicts = [c for c in ranked if c["direction"] == "USD"]
    else:
        conflicts = ranked[:4]
    conflicting_signals = [
        {
            "key": c["key"],
            "label": c["label"],
            "direction": c["direction"],
            "weight": c["weight"],
            "strength": c["strength"],
            "detail": c["detail"],
        }
        for c in conflicts
    ]

    return {
        "direction": direction,
        "market_bias": bias,
        "momentum_status": momentum_status,
        "trade_score": trade_score,
        "confidence": confidence,
        "usd_score": usd_score,
        "mxn_score": mxn_score,
        "net_score": net,
        "total_score": total,
        "key_drivers": key_drivers,
        "weighted_contributions": ranked,
        "conflicting_signals": conflicting_signals,
        "weights_version": WEIGHTS_VERSION,
        "trade_threshold": TRADE_THRESHOLD,
    }
