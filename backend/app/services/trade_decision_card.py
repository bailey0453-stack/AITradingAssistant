"""Top-of-dashboard trade decision card (decision support only).

Computes TRADE / WAIT / EXIT·INVALIDATED from explicit evidence gates.
Does **not** copy ``should_trade_now`` from decision_quality.
"""

from __future__ import annotations

from typing import Any, Optional

_ACTIONABLE = {"BUY_USD", "SELL_USD"}
_TRADE_GRADES = {"A", "A+"}
_MIN_CONFIDENCE = 70.0


def _f(v: Any) -> Optional[float]:
    try:
        return None if v is None else float(v)
    except (TypeError, ValueError):
        return None


def _direction_labels(direction: Optional[str]) -> dict:
    if direction == "SELL_USD":
        return {
            "bias": "SELL USD / BUY MXN",
            "prediction": "USD/MXN LOWER",
            "has_directional_forecast": True,
        }
    if direction == "BUY_USD":
        return {
            "bias": "BUY USD / SELL MXN",
            "prediction": "USD/MXN HIGHER",
            "has_directional_forecast": True,
        }
    return {
        "bias": "WAIT",
        "prediction": "No sufficiently strong setup",
        "has_directional_forecast": False,
    }


def _forecast_agrees(direction: Optional[str], spot: Optional[float], predicted: Optional[float]) -> bool:
    if direction not in _ACTIONABLE or spot is None or predicted is None:
        return False
    if direction == "BUY_USD":
        return predicted > spot
    return predicted < spot


def _historical_supports(dq: dict, mp: Optional[dict]) -> bool:
    similar = (dq or {}).get("similar_track_record") or {}
    avg_pnl = similar.get("similar_avg_pnl")
    if avg_pnl is None:
        avg_pnl = similar.get("avg_net_pnl_usd") or similar.get("avg_pnl_usd")
    avg_ret = similar.get("avg_return_pct")
    if avg_pnl is not None and float(avg_pnl) > 0:
        return True
    if avg_ret is not None and float(avg_ret) > 0:
        return True
    # Fallback: broader historical win-rate / measured expectancy when similar is thin.
    wr = similar.get("similar_win_rate")
    if wr is None:
        wr = similar.get("win_rate")
    if similar.get("enough_history") and wr is not None:
        return float(wr) > 50.0
    if mp:
        exp = (mp.get("expectancy") or {}).get("avg_net_pnl_usd")
        if exp is not None and float(exp) > 0:
            return True
    return False


def _event_risk_near(payload: dict, dq: dict) -> bool:
    if (dq or {}).get("high_impact_event_count"):
        return int(dq["high_impact_event_count"]) > 0
    comps = (dq or {}).get("components") or {}
    # event_risk component is 100 with zero high-impact events; drops by 25 each.
    er = comps.get("event_risk")
    if er is not None and float(er) < 100.0:
        return True
    ctx = payload.get("context") or {}
    upcoming = ctx.get("upcoming_events") or []
    for ev in upcoming:
        impact = str(ev.get("impact") or ev.get("importance") or "").lower()
        if impact in ("high", "3", "red"):
            return True
    return False


def _data_fresh_enough(payload: dict) -> bool:
    market = payload.get("market") or {}
    source = (market.get("source") or "").lower()
    if source in ("mock", "sample"):
        return False
    ms = payload.get("market_state") or {}
    if ms.get("is_stale") is True:
        return False
    # Cached is allowed for WAIT directional analysis, but not for TRADE.
    if ms.get("cached") is True and not ms.get("is_open"):
        # Closed market with fresh last session quote can still TRADE if not stale.
        age = ms.get("age_minutes")
        if age is not None and float(age) > 24 * 60:
            return False
    prov = payload.get("provenance") or {}
    freshness = (prov.get("freshness") or {}).get("market") or prov.get("market_freshness")
    if isinstance(freshness, str) and freshness.lower() in ("stale", "unavailable", "degraded"):
        return False
    if market.get("usdmxn") is None and market.get("rate") is None:
        return False
    return True


def _invalidated(
    direction: Optional[str],
    spot: Optional[float],
    long_bailout: Optional[float],
    short_bailout: Optional[float],
    *,
    explicit: bool = False,
) -> bool:
    if explicit:
        return True
    if direction == "BUY_USD" and spot is not None and long_bailout is not None:
        return spot < long_bailout
    if direction == "SELL_USD" and spot is not None and short_bailout is not None:
        return spot > short_bailout
    return False


def _why_wait(reasons: list[str]) -> str:
    if not reasons:
        return "Directional prediction exists, but evidence is not strong enough to trade."
    return "; ".join(reasons[:4]).rstrip(".") + "."


def build_trade_decision_card(payload: dict) -> dict:
    """Build the top decision card from an ``/analysis/usdmxn`` payload."""
    direction = payload.get("direction")
    grade = payload.get("opportunity_grade") or payload.get("grade")
    confidence = _f(payload.get("confidence")) or 0.0
    market = payload.get("market") or {}
    spot = _f(market.get("usdmxn") if market.get("usdmxn") is not None else market.get("rate"))
    dq = payload.get("decision_quality") or {}
    mp = payload.get("model_performance")
    tl = payload.get("topline_forecast") or {}

    labels = _direction_labels(direction if direction in _ACTIONABLE else None)
    # Keep directional forecast even when WAIT — use model direction if present.
    if direction in _ACTIONABLE:
        labels = _direction_labels(direction)

    rate_4h = None
    rate_eod = None
    for entry in tl.get("path") or tl.get("horizons") or []:
        h = (entry.get("horizon") or "").lower()
        if h in ("4 hours", "4h", "1-4 hours") and rate_4h is None:
            rate_4h = _f(entry.get("expected_rate"))
        if h in ("end of day", "eod") and rate_eod is None:
            rate_eod = _f(entry.get("expected_rate"))
    # Named keys used by topline_forecast.build
    if rate_4h is None:
        rate_4h = _f((tl.get("four_hours") or {}).get("expected_rate") if isinstance(tl.get("four_hours"), dict) else tl.get("four_hours"))
    if rate_eod is None:
        rate_eod = _f((tl.get("end_of_day") or {}).get("expected_rate") if isinstance(tl.get("end_of_day"), dict) else tl.get("end_of_day"))
    # Path list with horizon labels from build()
    for key, dest in (("4_hours", "4h"), ("end_of_day", "eod"), ("four_hour", "4h")):
        node = tl.get(key)
        if isinstance(node, dict):
            val = _f(node.get("expected_rate"))
            if dest == "4h" and rate_4h is None:
                rate_4h = val
            if dest == "eod" and rate_eod is None:
                rate_eod = val

    long_bailout = _f(tl.get("long_usd_bailout"))
    short_bailout = _f(tl.get("short_usd_bailout"))
    if long_bailout is None:
        long_bailout = _f((tl.get("bailouts") or {}).get("long_usd"))
    if short_bailout is None:
        short_bailout = _f((tl.get("bailouts") or {}).get("short_usd"))
    # Fall back to recommendation stop as the primary invalidation side.
    stop = _f(payload.get("stop"))
    if direction == "BUY_USD" and long_bailout is None:
        long_bailout = stop
    if direction == "SELL_USD" and short_bailout is None:
        short_bailout = stop

    invalidation_level = None
    invalidation_label = "INVALIDATION"
    if direction == "BUY_USD":
        invalidation_level = long_bailout
        invalidation_label = "INVALID BELOW"
    elif direction == "SELL_USD":
        invalidation_level = short_bailout
        invalidation_label = "INVALID ABOVE"

    predicted_primary = rate_4h if rate_4h is not None else rate_eod
    forecast_ok = _forecast_agrees(direction, spot, predicted_primary)
    # Also accept agreement with end-of-day if 4h missing
    if not forecast_ok and rate_eod is not None:
        forecast_ok = _forecast_agrees(direction, spot, rate_eod)

    ev = _f((dq.get("expected_value") or {}).get("expected_value_usd"))
    if ev is None:
        ev = _f(dq.get("expected_value_usd"))

    explicit_invalid = bool(
        payload.get("invalidated")
        or (payload.get("status") or "").upper() == "INVALIDATED"
        or (dq.get("status") or "").upper() == "INVALIDATED"
    )
    is_invalid = _invalidated(
        direction, spot, long_bailout, short_bailout, explicit=explicit_invalid
    )

    wait_reasons: list[str] = []
    actionable = direction in _ACTIONABLE
    grade_ok = (grade or "") in _TRADE_GRADES
    conf_ok = confidence >= _MIN_CONFIDENCE
    ev_ok = ev is not None and ev > 0
    hist_ok = _historical_supports(dq, mp)
    event_near = _event_risk_near(payload, dq)
    fresh_ok = _data_fresh_enough(payload)

    if not actionable:
        wait_reasons.append("No actionable BUY_USD/SELL_USD signal")
    if actionable and not grade_ok:
        wait_reasons.append(f"Grade {grade or 'n/a'} (need A or A+)")
    if actionable and not conf_ok:
        wait_reasons.append(f"Confidence {confidence:.0f} below 70")
    if actionable and not ev_ok:
        wait_reasons.append(
            "Negative expected value" if ev is not None and ev <= 0 else "Expected value unavailable or not positive"
        )
    if actionable and not hist_ok:
        wait_reasons.append("Historical comparable setups do not support acting")
    if event_near:
        wait_reasons.append("A high-impact event is approaching")
    if not fresh_ok:
        wait_reasons.append("Required market inputs are stale or insufficient")
    if actionable and not forecast_ok:
        wait_reasons.append("Primary signal and forecast direction disagree")

    if is_invalid and actionable:
        action = "EXIT / INVALIDATED"
        visual = "red"
        why = (
            f"Spot {spot:g} crossed the invalidation level "
            f"({invalidation_label} {invalidation_level:g})."
            if spot is not None and invalidation_level is not None
            else "Recommendation is invalidated."
        )
    elif (
        actionable
        and grade_ok
        and conf_ok
        and ev_ok
        and hist_ok
        and not event_near
        and fresh_ok
        and forecast_ok
        and not is_invalid
    ):
        action = "TRADE"
        visual = "green"
        why = (
            f"Grade {grade}, confidence {confidence:.0f}, positive expected value, "
            "and historical support align with the forecast."
        )
    else:
        action = "WAIT"
        visual = "yellow"
        why = _why_wait(wait_reasons)

    return {
        "action": action,
        "visual": visual,
        "direction": direction,
        "bias": labels["bias"],
        "prediction": labels["prediction"],
        "has_directional_forecast": labels["has_directional_forecast"] or actionable,
        "spot": round(spot, 4) if spot is not None else None,
        "predicted_4h": round(rate_4h, 4) if rate_4h is not None else None,
        "predicted_eod": round(rate_eod, 4) if rate_eod is not None else None,
        "invalidation_label": invalidation_label,
        "invalidation_level": round(invalidation_level, 4) if invalidation_level is not None else None,
        "why": why,
        "wait_reasons": wait_reasons,
        "gates": {
            "actionable": actionable,
            "grade_ok": grade_ok,
            "confidence_ok": conf_ok,
            "expected_value_ok": ev_ok,
            "historical_support": hist_ok,
            "no_event_risk": not event_near,
            "data_fresh": fresh_ok,
            "forecast_agrees": forecast_ok,
            "not_invalidated": not is_invalid,
        },
        "inputs": {
            "grade": grade,
            "confidence": confidence,
            "expected_value_usd": ev,
            "should_trade_now_ignored": dq.get("should_trade_now"),
        },
        "disclaimer": "Decision support only — not an order or automated trade.",
    }
