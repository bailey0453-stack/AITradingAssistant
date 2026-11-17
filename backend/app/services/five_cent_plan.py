"""User-selected five Mexican cent target/stop; indicative levels, never orders.

These distances describe the FIX midpoint, not net profit or calibrated barrier
hit probabilities. Existing forecast evidence remains separate from this plan.
"""
from math import isfinite

from app.services.hedge_pnl import hedge_pnl_detail, parse_fix_quote

DISTANCE_MXN = 0.05


def settings():
    return {
        "target_cents": 5, "stop_cents": 5, "max_hold_minutes": 20,
        "basis": "FIX midpoint movement from this setup's quote",
        "status": "unavailable", "target_rate": None, "stop_rate": None,
        "entry_rate": None, "exit_deadline": None,
        "target_net_pnl_usd": None, "stop_net_pnl_usd": None,
        "target_before_stop_probability": None,
        "note": "Signal levels only; no orders are placed. If you enter, keep that setup's original target, stop and deadline; new quotes show new setups.",
    }


def apply(result, direction, fix):
    """Attach levels to a supported numeric forecast; preserve all WAIT gates."""
    plan = settings()
    result["five_cent_plan"] = plan
    parsed = parse_fix_quote(fix)
    expected = result.get("expected_rate")
    if parsed is None or direction not in {"BUY_USD", "SELL_USD"} or expected is None:
        return result
    bid, ask = parsed
    if not all(isfinite(v) for v in (bid, ask, expected)):
        return result
    mid = (bid + ask) / 2
    sign = 1 if direction == "BUY_USD" else -1
    target, stop = mid + sign * DISTANCE_MXN, mid - sign * DISTANCE_MXN
    target_pnl = hedge_pnl_detail(direction, target, fix)
    stop_pnl = hedge_pnl_detail(direction, stop, fix)
    if target_pnl is None or stop_pnl is None:
        return result
    plan.update(
        status="conditional", direction=direction, reference_rate=round(mid, 6),
        target_rate=round(target, 6), stop_rate=round(stop, 6),
        entry_rate=target_pnl["entry_rate"], exit_deadline=result.get("forecast_for"),
        target_exit_rate=target_pnl["exit_rate"], stop_exit_rate=stop_pnl["exit_rate"],
        target_net_pnl_usd=target_pnl["net_pnl_usd"],
        stop_net_pnl_usd=stop_pnl["net_pnl_usd"],
        cost_basis="Estimated $100K net with current FIX spread held at exit and $40 round-trip fees; excludes slippage and stop gaps.",
        evidence="The chance of reaching this target before the stop is not yet established.",
    )
    reasons = result.setdefault("wait_reasons", [])
    if sign * (expected - mid) < DISTANCE_MXN - 1e-9:
        reasons.append("The remaining forecast move is smaller than the five-cent target.")
    if target_pnl["net_pnl_usd"] <= 0:
        reasons.append("A five-cent move does not cover the FIX spread and round-trip fees.")
    if reasons:
        result["action"] = "WAIT"
    elif result.get("action") != "WAIT":
        plan["status"] = "signal"
    return result
