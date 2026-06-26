"""Market regime detection for USD/MXN.

Phase 3.5 adds a layer on top of the signal engine: *what kind of market are we
in right now?* The same evidence the weighting engine scores (macro deltas,
news, calendar, momentum, volatility) is re-read here to classify the dominant
and secondary regime, with a confidence.

This is intentionally transparent and deterministic — each regime accumulates a
score from named pieces of evidence, and we expose the rationale. It does not
replace anything; the analyzer calls it for context and grading.

Regimes:
    Risk On, Risk Off, Fed Driven, Banxico Driven, Inflation Driven, Oil Driven,
    Trade War, Political Risk, Low Volatility, High Volatility, Range Bound,
    Trending
"""

from __future__ import annotations

from app.services.market_data import MarketData
from app.services.signal_weights import event_signal_key, news_category

REGIMES = (
    "Risk On",
    "Risk Off",
    "Fed Driven",
    "Banxico Driven",
    "Inflation Driven",
    "Oil Driven",
    "Trade War",
    "Political Risk",
    "Low Volatility",
    "High Volatility",
    "Range Bound",
    "Trending",
)

_IMPORTANCE = {"high": 1.0, "medium": 0.6, "low": 0.3}
_MOMENTUM_NORM = 0.05  # USD/MXN move that counts as a full-strength trend


def detect_regime(
    market: MarketData,
    news: list[dict] | None = None,
    calendar: list[dict] | None = None,
    momentum: dict | None = None,
    signal: dict | None = None,
) -> dict:
    """Classify the current market regime.

    Returns ``{primary, secondary, confidence (0..100), scores, rationale}``.
    """
    d = market.drivers or {}
    scores: dict[str, float] = {r: 0.0 for r in REGIMES}
    rationale: list[str] = []

    def bump(regime: str, amount: float, why: str | None = None) -> None:
        if amount <= 0:
            return
        scores[regime] += amount
        if why:
            rationale.append(why)

    # --- Volatility regime (VIX level + change) ---
    vix = market.vix
    vix_delta = float(d.get("vix_delta", 0.0))
    if vix is not None:
        if vix >= 20:
            bump("High Volatility", 1.0 + (vix - 20) / 10.0, f"VIX elevated at {vix}")
            bump("Risk Off", 0.8)
        elif vix < 14:
            bump("Low Volatility", 1.0 + (14 - vix) / 6.0, f"VIX subdued at {vix}")
            bump("Risk On", 0.4)
    if vix_delta >= 1.0:
        bump("High Volatility", vix_delta / 3.0, "Volatility rising")
        bump("Risk Off", vix_delta / 4.0)
    elif vix_delta <= -1.0:
        bump("Low Volatility", abs(vix_delta) / 3.0)
        bump("Risk On", abs(vix_delta) / 4.0)

    # --- Risk on / off (equities + gold) ---
    sp_delta = float(d.get("sp_delta", 0.0))
    if sp_delta > 5:
        bump("Risk On", min(1.5, sp_delta / 40.0 + 0.3), "Equity futures higher (risk-on)")
    elif sp_delta < -5:
        bump("Risk Off", min(1.5, abs(sp_delta) / 40.0 + 0.3), "Equity futures lower (risk-off)")
    gold_delta = float(d.get("gold_delta", 0.0))
    if gold_delta > 10 and sp_delta < 0:
        bump("Risk Off", min(1.0, gold_delta / 25.0), "Gold bid while equities soft")

    # --- Oil-driven ---
    oil_delta = float(d.get("oil_delta", 0.0))
    if abs(oil_delta) > 1.0:
        bump("Oil Driven", min(1.5, abs(oil_delta) / 2.5), f"Oil moving sharply ({market.oil})")

    # --- Event-driven regimes (calendar: near upcoming or recently released) ---
    for ev in calendar or []:
        imp = _IMPORTANCE.get(str(ev.get("importance", "low")).lower(), 0.3)
        if imp < 0.6:
            continue
        key = event_signal_key(ev.get("event", ""))
        name = ev.get("event", "event")
        if key in ("fed_rate_decision",):
            bump("Fed Driven", imp + 0.4, f"Fed event in focus: {name}")
        elif key in ("banxico_rate_decision",):
            bump("Banxico Driven", imp + 0.4, f"Banxico event in focus: {name}")
        elif key in ("us_cpi", "us_ppi", "mexico_cpi"):
            bump("Inflation Driven", imp + 0.2, f"Inflation print in focus: {name}")

    # --- News-driven regimes ---
    trade_news = 0.0
    political_news = 0.0
    for item in news or []:
        cat = news_category(item)
        imp = _IMPORTANCE.get(str(item.get("importance", "low")).lower(), 0.3)
        if cat == "trade_tariff_news":
            trade_news += imp
        elif cat == "political_news":
            political_news += imp
    if trade_news > 0:
        bump("Trade War", min(2.0, trade_news), "Trade/tariff headlines active")
    if political_news > 0:
        bump("Political Risk", min(2.0, political_news), "Political headlines active")

    # --- Trending vs range-bound (real momentum if available) ---
    trend_strength = 0.0
    if momentum and momentum.get("change") is not None:
        trend_strength = min(1.5, abs(float(momentum["change"])) / _MOMENTUM_NORM)
    elif signal:
        # Fall back to conviction of the weighted signal as a trend proxy.
        trend_strength = min(1.2, (signal.get("trade_score") or 0) / 100.0 * 1.2)
    if trend_strength >= 0.6:
        bump("Trending", trend_strength, "Directional momentum present")
    else:
        bump("Range Bound", 0.6 - trend_strength + 0.4, "No strong directional momentum")

    # --- Resolve primary / secondary / confidence ---
    ranked = sorted(scores.items(), key=lambda kv: kv[1], reverse=True)
    ranked = [(r, round(s, 2)) for r, s in ranked if s > 0]

    if not ranked:
        return {
            "primary": "Range Bound",
            "secondary": None,
            "confidence": 30.0,
            "scores": {},
            "rationale": ["No dominant regime signal; treating as range-bound."],
        }

    primary, primary_score = ranked[0]
    secondary = ranked[1][0] if len(ranked) > 1 else None
    total = sum(s for _, s in ranked) or 1.0
    confidence = round(min(95.0, primary_score / total * 100.0), 1)

    return {
        "primary": primary,
        "secondary": secondary,
        "confidence": confidence,
        "scores": dict(ranked),
        "rationale": rationale[:6],
    }
