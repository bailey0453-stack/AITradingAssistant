"""Topline Rate Forecast (decision support only — never trade execution).

Adds estimated net P/L for a $100k USD hedge using the live Centroid FIX
bid/ask as the executable entry, preserving the current FIX spread at each
forecast horizon, and deducting $20 per $100k on both entry and exit.
"""

from __future__ import annotations

from typing import Optional

from app.services.fix.provider import get_fix_quote

_INTRADAY = "1-4 hours"
_EOD = "End of day"
_MULTIDAY = "1-2 days"
_DEFAULT_STOP_PCT = 0.002
_HEDGE_USD = 100_000.0
_FEE_PER_SIDE_USD = 20.0


def _move_pct(rate: Optional[float], spot: Optional[float]) -> Optional[float]:
    if rate is None or not spot:
        return None
    return round((rate / spot - 1) * 100, 3)


def _hedge_pnl(direction: str, forecast_mid: Optional[float], fix: dict | None) -> Optional[float]:
    if direction not in ("BUY_USD", "SELL_USD") or forecast_mid is None or not fix:
        return None
    try:
        bid = float(fix["bid"])
        ask = float(fix["ask"])
    except (KeyError, TypeError, ValueError):
        return None
    if bid <= 0 or ask <= 0 or ask < bid or forecast_mid <= 0:
        return None
    half_spread = (ask - bid) / 2.0
    future_bid = forecast_mid - half_spread
    future_ask = forecast_mid + half_spread
    if future_bid <= 0 or future_ask <= 0:
        return None
    fees = 2.0 * _FEE_PER_SIDE_USD
    if direction == "SELL_USD":
        mxn_received = _HEDGE_USD * bid
        usd_to_rebuy = mxn_received / future_ask
        gross = usd_to_rebuy - _HEDGE_USD
    else:
        mxn_cost = _HEDGE_USD * ask
        usd_recovered = mxn_cost / future_bid
        gross = _HEDGE_USD - usd_recovered
    return round(gross - fees, 2)


def _display_grade(grade: Optional[str], pnl: Optional[float]) -> Optional[str]:
    """Decorate the per-horizon dashboard grade with its net $100k hedge P/L."""
    if grade is None or pnl is None:
        return grade
    sign = "+" if pnl >= 0 else "-"
    return f"{grade} · Net {sign}${abs(pnl):,.0f}"


def _entry(label: str, rate: Optional[float], bias: Optional[str], confidence: Optional[float], spot: Optional[float], *, grade: Optional[str] = None, direction: str = "NO_TRADE", fix: dict | None = None) -> dict:
    pnl = _hedge_pnl(direction, rate, fix)
    return {
        "horizon": label,
        "expected_rate": round(rate, 4) if rate is not None else None,
        "bias": bias or "HOLD",
        "confidence": round(float(confidence), 1) if confidence is not None else 0.0,
        "expected_move_pct": _move_pct(rate, spot),
        "grade": _display_grade(grade, pnl),
        "opportunity_grade": grade,
        "hedge_pnl_usd": pnl,
    }


def _bailouts(spot: Optional[float], direction: str, primary_stop: Optional[float]) -> tuple[Optional[float], Optional[float]]:
    if not spot or direction not in ("BUY_USD", "SELL_USD"):
        return None, None
    stop_pct = abs(spot - primary_stop) / spot if primary_stop else _DEFAULT_STOP_PCT
    long_bailout = round(spot * (1 - stop_pct), 4)
    short_bailout = round(spot * (1 + stop_pct), 4)
    if direction == "BUY_USD" and primary_stop:
        long_bailout = round(primary_stop, 4)
    elif direction == "SELL_USD" and primary_stop:
        short_bailout = round(primary_stop, 4)
    return long_bailout, short_bailout


def _explanation(direction: str, spot: Optional[float], long_bailout: Optional[float], short_bailout: Optional[float], *, grade: Optional[str] = None) -> str:
    grade_bit = f" (Grade {grade})" if grade else ""
    if not spot:
        return "Market data unavailable — no expected rate path or bailout levels. Decision support only."
    if direction == "BUY_USD":
        return f"Primary lean is BUY_USD{grade_bit}: the expected path projects above spot {spot:g}. The long thesis is invalidated below {long_bailout:g} (Long USD bailout); the reverse short-thesis level is {short_bailout:g}. Estimates — decision support only, not execution."
    if direction == "SELL_USD":
        return f"Primary lean is SELL_USD{grade_bit}: the expected path projects below spot {spot:g}. The short thesis is invalidated above {short_bailout:g} (Short USD bailout); the reverse long-thesis level is {long_bailout:g}. Estimates — decision support only, not execution."
    if direction == "HOLD":
        return f"Neutral HOLD bias{grade_bit} around spot {spot:g}: expected rates are range-bound and bailout levels are N/A until conviction rises. Decision support only."
    if direction == "NO_TRADE":
        return f"Stand aside (NO_TRADE){grade_bit} around spot {spot:g}: no committed bias; bailout levels are N/A. Decision support only."
    return f"No directional edge around spot {spot:g}: expected rates are range-bound and bailout levels are N/A. Decision support only."


def build(payload: dict) -> dict:
    market = payload.get("market") or {}
    spot = market.get("usdmxn")
    direction = payload.get("direction") or "NO_TRADE"
    overall_grade = payload.get("opportunity_grade")
    primary_stop = payload.get("stop")
    by_name = {h.get("horizon"): h for h in (payload.get("time_horizons") or [])}
    intraday = by_name.get(_INTRADAY, {})
    eod = by_name.get(_EOD, {})
    multiday = by_name.get(_MULTIDAY, {})
    intraday_target = intraday.get("target")
    intraday_bias = intraday.get("bias", "HOLD")
    intraday_conf = intraday.get("confidence", 0.0)

    try:
        fix = get_fix_quote()
    except Exception:
        fix = None

    def interp(frac: float) -> Optional[float]:
        if intraday_target is None or not spot:
            return None
        return spot + frac * (intraday_target - spot)

    kw = {"grade": overall_grade, "direction": direction, "fix": fix}
    horizons = [
        _entry("1 hour", interp(0.25), intraday_bias, intraday_conf, spot, **kw),
        _entry("2 hours", interp(0.5), intraday_bias, intraday_conf, spot, **kw),
        _entry("4 hours", interp(1.0), intraday_bias, intraday_conf, spot, **kw),
        _entry(_EOD, eod.get("target"), eod.get("bias", "HOLD"), eod.get("confidence", 0.0), spot, **kw),
        _entry("24 hours", multiday.get("target"), multiday.get("bias", "HOLD"), multiday.get("confidence", 0.0), spot, **kw),
    ]
    long_bailout, short_bailout = _bailouts(spot, direction, primary_stop)
    fix_bid = fix.get("bid") if fix else None
    fix_ask = fix.get("ask") if fix else None
    entry_rate = fix_bid if direction == "SELL_USD" else fix_ask if direction == "BUY_USD" else None
    return {
        "now": round(spot, 4) if spot is not None else None,
        "opportunity_grade": overall_grade,
        "horizons": horizons,
        "long_usd_bailout": long_bailout,
        "short_usd_bailout": short_bailout,
        "hedge": {
            "notional_usd": int(_HEDGE_USD),
            "fee_per_side_usd": _FEE_PER_SIDE_USD,
            "round_trip_fees_usd": 2 * _FEE_PER_SIDE_USD,
            "fix_bid": fix_bid,
            "fix_ask": fix_ask,
            "fix_spread": fix.get("spread") if fix else None,
            "entry_rate": entry_rate,
            "entry_side": "FIX bid" if direction == "SELL_USD" else "FIX ask" if direction == "BUY_USD" else None,
            "available": entry_rate is not None,
        },
        "explanation": _explanation(direction, spot, long_bailout, short_bailout, grade=overall_grade),
    }
