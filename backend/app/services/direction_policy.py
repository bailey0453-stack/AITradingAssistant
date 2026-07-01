"""Direction vs stand-aside policy (Phase B recommendation logic).

Always express a directional bias (BUY_USD, SELL_USD, or HOLD) from weighted
evidence. Reserve ``NO_TRADE`` for exceptional stand-aside cases only:

- Imminent high-impact macro event with a neutral HOLD bias (two-way risk).
- Market-data unavailable (handled in the analysis router, not here).

Direction (best estimate) and confidence (conviction) stay separate; ``NO_TRADE``
does not replace a weak lean — that is ``HOLD``. A flat tape with no firing
signals still returns ``HOLD`` with low confidence.
"""

from __future__ import annotations

from app.services.signal_weights import DIRECTION_EPSILON, TRADE_THRESHOLD

# Hours before a high-impact release that can force stand-aside when bias is HOLD.
_CRITICAL_EVENT_HOURS = 24.0


def conviction_tier(net_score: float) -> str:
    """Map |net weighted score| to a plain conviction bucket."""
    net = abs(float(net_score or 0.0))
    if net >= TRADE_THRESHOLD:
        return "high"
    if net >= 2.0:
        return "medium"
    if net >= DIRECTION_EPSILON:
        return "low"
    return "none"


def _critical_events_within_hours(
    upcoming_events: list[dict] | None, hours: float
) -> list[dict]:
    out: list[dict] = []
    for ev in upcoming_events or []:
        if str(ev.get("importance", "")).lower() != "high":
            continue
        h = ev.get("hours_away")
        if h is not None and float(h) <= hours:
            out.append(ev)
    return out


def apply_stand_aside(
    signal: dict,
    *,
    upcoming_events: list[dict] | None = None,
) -> tuple[dict, str | None]:
    """Override to ``NO_TRADE`` only when stand-aside is compelled.

    Returns ``(signal, stand_aside_reason)`` — reason is ``None`` when the
    directional bias stands.
    """
    sb = signal.get("signal_breakdown") or {}
    direction = signal.get("direction")

    if direction == "HOLD":
        critical = _critical_events_within_hours(upcoming_events, _CRITICAL_EVENT_HOURS)
        if critical:
            name = critical[0].get("event") or "major macro release"
            hours = critical[0].get("hours_away")
            when = f"in ~{hours:.0f}h" if hours is not None else "imminently"
            reason = (
                f"Imminent high-impact event ({name}, {when}) with neutral bias — "
                f"stand aside until the release clears two-way risk."
            )
            return _as_no_trade(signal, reason), reason

    return signal, None


def _as_no_trade(signal: dict, reason: str) -> dict:
    out = dict(signal)
    out["direction"] = "NO_TRADE"
    out["market_bias"] = "Stand aside"
    out["momentum_status"] = "Neutral / event risk"
    out["is_actionable"] = False
    out["stand_aside"] = True
    out["stand_aside_reason"] = reason
    # No trade plan without a committed bias.
    for key in ("target", "stretch_target", "stop", "invalidation_level"):
        out[key] = None
    out["expected_move"] = "flat / range-bound"
    conf = float(out.get("confidence") or 0.0)
    out["confidence"] = round(min(conf, 35.0), 1)
    return out


def build_direction_reasoning(
    signal: dict,
    *,
    bullish_factors: list[str] | None = None,
    bearish_factors: list[str] | None = None,
    agree: list[str] | None = None,
    disagree: list[str] | None = None,
    stand_aside_reason: str | None = None,
) -> dict:
    """Structured reasoning: bias, conviction, supporting vs opposing evidence."""
    direction = signal.get("direction")
    sb = signal.get("signal_breakdown") or {}
    net = float(sb.get("net_score") if sb.get("net_score") is not None else signal.get("net_score") or 0.0)
    tier = conviction_tier(net)

    supporting: list[str] = []
    opposing: list[str] = []

    ranked = signal.get("weighted_contributions") or []
    if direction == "BUY_USD":
        supporting = [c["label"] for c in ranked if c.get("direction") == "USD"][:5]
        opposing = [c["label"] for c in ranked if c.get("direction") == "MXN"][:5]
    elif direction == "SELL_USD":
        supporting = [c["label"] for c in ranked if c.get("direction") == "MXN"][:5]
        opposing = [c["label"] for c in ranked if c.get("direction") == "USD"][:5]
    elif direction == "HOLD":
        supporting = list(bullish_factors or [])[:3] + list(bearish_factors or [])[:3]
        opposing = [c["label"] for c in (signal.get("conflicting_signals") or [])[:4]]
    elif direction == "NO_TRADE":
        opposing = [c["label"] for c in (signal.get("conflicting_signals") or [])[:4]]

    if agree:
        supporting = list(dict.fromkeys(list(agree) + supporting))[:6]
    if disagree:
        opposing = list(dict.fromkeys(list(disagree) + opposing))[:6]

    bias_label = {
        "BUY_USD": "BUY USD (USD/MXN higher)",
        "SELL_USD": "SELL USD (USD/MXN lower)",
        "HOLD": "HOLD / NEUTRAL",
        "NO_TRADE": "NO TRADE (stand aside)",
    }.get(direction, direction)

    summary_parts = [f"Directional bias: {bias_label}."]
    conf = signal.get("confidence")
    if conf is not None:
        summary_parts.append(f"Confidence {conf}/100 ({tier} conviction).")
    if signal.get("is_actionable"):
        summary_parts.append(
            f"Actionable edge (|net| >= {TRADE_THRESHOLD:g} weighted score)."
        )
    elif direction == "HOLD":
        summary_parts.append(
            f"Bias only — net score {net:+.2g} is below the {TRADE_THRESHOLD:g} action threshold."
        )
    if stand_aside_reason:
        summary_parts.append(stand_aside_reason)

    return {
        "directional_bias": direction,
        "bias_label": bias_label,
        "confidence": signal.get("confidence"),
        "conviction_tier": tier,
        "is_actionable": bool(signal.get("is_actionable")),
        "action_threshold": TRADE_THRESHOLD,
        "net_score": net,
        "supporting_signals": supporting,
        "opposing_signals": opposing,
        "stand_aside": direction == "NO_TRADE",
        "stand_aside_reason": stand_aside_reason,
        "summary": " ".join(summary_parts),
    }
