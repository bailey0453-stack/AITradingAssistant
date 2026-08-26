"""FIX-based $100k hedge P/L (decision support only — never trade execution).

Same economics as the topline forecast:

- $100,000 notional
- $20 entry + $20 exit ($40 round-trip)
- SELL_USD enters at the executable FIX bid and closes at the opposite ask
- BUY_USD enters at the executable FIX ask and closes at the opposite bid
- forecast / historical model moves are re-anchored onto the FIX midpoint so a
  difference between the model spot feed and FIX mid is never counted as P/L
- the captured FIX spread is preserved at the exit mid

Historical records without a stored executable FIX bid/ask return unavailable
P/L rather than inventing a quote. Model mid alone is not enough to reconstruct
the executable spread.
"""

from __future__ import annotations

from typing import Any, Optional

HEDGE_USD = 100_000.0
FEE_PER_SIDE_USD = 20.0
ROUND_TRIP_FEES_USD = 2.0 * FEE_PER_SIDE_USD  # 40
_ACTIONABLE = frozenset({"BUY_USD", "SELL_USD"})


def parse_fix_quote(fix: Optional[dict]) -> Optional[tuple[float, float]]:
    """Return ``(bid, ask)`` when both sides are a usable executable quote."""
    if not fix:
        return None
    try:
        bid = float(fix["bid"])
        ask = float(fix["ask"])
    except (KeyError, TypeError, ValueError):
        return None
    if bid <= 0 or ask <= 0 or ask < bid:
        return None
    return bid, ask


def reanchor_to_fix(
    forecast_rate: Optional[float],
    model_spot: Optional[float],
    fix: Optional[dict],
) -> Optional[float]:
    """Apply the model's absolute move to the FIX midpoint.

    If FIX is missing the original rate is returned unchanged so callers that
    only need a display mid can still function; P/L helpers still require FIX.
    """
    if forecast_rate is None:
        return None
    parsed = parse_fix_quote(fix)
    if not model_spot or parsed is None:
        return float(forecast_rate)
    bid, ask = parsed
    fix_mid = (bid + ask) / 2.0
    return fix_mid + (float(forecast_rate) - float(model_spot))


def hedge_pnl_detail(
    direction: str,
    forecast_mid: Optional[float],
    fix: Optional[dict],
) -> Optional[dict[str, Any]]:
    """Net P/L and executable exit rate for a $100k hedge to ``forecast_mid``."""
    parsed = parse_fix_quote(fix)
    if direction not in _ACTIONABLE or forecast_mid is None or parsed is None:
        return None
    bid, ask = parsed
    if forecast_mid <= 0:
        return None
    half_spread = (ask - bid) / 2.0
    future_bid = forecast_mid - half_spread
    future_ask = forecast_mid + half_spread
    if future_bid <= 0 or future_ask <= 0:
        return None
    fees = ROUND_TRIP_FEES_USD
    if direction == "SELL_USD":
        mxn_received = HEDGE_USD * bid
        usd_to_rebuy = mxn_received / future_ask
        gross = usd_to_rebuy - HEDGE_USD
        entry_rate = bid
        exit_rate = future_ask
    else:
        mxn_cost = HEDGE_USD * ask
        usd_recovered = mxn_cost / future_bid
        gross = HEDGE_USD - usd_recovered
        entry_rate = ask
        exit_rate = future_bid
    return {
        "gross_pnl_usd": round(gross, 2),
        "fees_usd": fees,
        "net_pnl_usd": round(gross - fees, 2),
        "entry_rate": round(entry_rate, 6),
        "exit_rate": round(exit_rate, 4),
    }


def net_hedge_pnl_usd(
    direction: str,
    forecast_mid: Optional[float],
    fix: Optional[dict],
) -> Optional[float]:
    detail = hedge_pnl_detail(direction, forecast_mid, fix)
    return None if detail is None else detail["net_pnl_usd"]


def stored_fix_quote(reco: Any) -> Optional[dict[str, float]]:
    """Executable FIX bid/ask captured on the recommendation, if present.

    Inspects recommendation columns first, then optional JSON metadata
    (``primary_trade_plan`` / ``strategist``). Does not invent a quote from the
    model spot feed — mid-only history cannot reconstruct the executable spread.
    """
    candidates: list[Any] = [reco]
    plan = getattr(reco, "primary_trade_plan", None)
    if isinstance(plan, dict):
        candidates.append(plan)
        hedge = plan.get("hedge") or plan.get("fix") or plan.get("fix_quote")
        if isinstance(hedge, dict):
            candidates.append(hedge)
    strategist = getattr(reco, "strategist", None)
    if isinstance(strategist, dict):
        candidates.append(strategist)
        hedge = strategist.get("hedge") or strategist.get("fix") or strategist.get("fix_quote")
        if isinstance(hedge, dict):
            candidates.append(hedge)

    for src in candidates:
        bid = _attr_or_key(src, "fix_bid")
        ask = _attr_or_key(src, "fix_ask")
        if bid is None:
            bid = _attr_or_key(src, "bid")
        if ask is None:
            ask = _attr_or_key(src, "ask")
        parsed = parse_fix_quote({"bid": bid, "ask": ask})
        if parsed is not None:
            return {"bid": parsed[0], "ask": parsed[1]}
    return None


def historical_horizon_pnl(
    direction: str,
    model_spot: Optional[float],
    eval_spot: Optional[float],
    fix: Optional[dict],
) -> dict[str, Any]:
    """P/L at a scored horizon using the stored FIX quote and outcome spot.

    The observed model move (``eval_spot - model_spot``) is applied to the FIX
    midpoint. ``pricing_basis`` is ``fix`` when an executable quote exists,
    otherwise ``unavailable`` (null P/L). There is no mid-only fallback.
    """
    empty = {"net_pnl_usd": None, "exit_rate": None, "pricing_basis": "unavailable"}
    if direction not in _ACTIONABLE:
        return {**empty, "pricing_basis": None}
    if eval_spot is None or model_spot is None:
        return empty
    if parse_fix_quote(fix) is None:
        return empty
    exit_mid = reanchor_to_fix(eval_spot, model_spot, fix)
    detail = hedge_pnl_detail(direction, exit_mid, fix)
    if detail is None:
        return empty
    return {
        "net_pnl_usd": detail["net_pnl_usd"],
        "exit_rate": detail["exit_rate"],
        "pricing_basis": "fix",
    }


def _attr_or_key(src: Any, name: str) -> Any:
    if isinstance(src, dict):
        return src.get(name)
    return getattr(src, name, None)
