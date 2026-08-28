"""Topline Rate Forecast (decision support only — never trade execution).

Adds estimated net P/L for a $100k USD hedge using the live Centroid FIX
bid/ask as the executable entry. Forecast moves are re-anchored from the model
spot to the current FIX midpoint so feed-basis differences are not counted as
hedge P/L. Preserves the current FIX spread at each forecast horizon and
deducts $20 per $100k on both entry and exit.

Forecast horizons are market-session aware.  In particular, Friday intraday
horizons never extend beyond the 21:00 UTC FX close, and weekend hours are not
presented as if they were executable trading hours.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Optional

from app.services.fix.provider import get_fix_quote
from app.services.hedge_pnl import (
    FEE_PER_SIDE_USD as _FEE_PER_SIDE_USD,
    HEDGE_USD as _HEDGE_USD,
    net_hedge_pnl_usd,
    reanchor_to_fix,
)
from app.services.market_hours import get_market_state

_INTRADAY = "1-4 hours"
_EOD = "End of day"
_MULTIDAY = "1-2 days"
_DEFAULT_STOP_PCT = 0.002
_FX_CLOSE_HOUR_UTC = 21


def _move_pct(rate: Optional[float], spot: Optional[float]) -> Optional[float]:
    if rate is None or not spot:
        return None
    return round((rate / spot - 1) * 100, 3)


def _display_grade(grade: Optional[str], pnl: Optional[float]) -> Optional[str]:
    if grade is None or pnl is None:
        return grade
    sign = "+" if pnl >= 0 else "-"
    return f"{grade} · Net {sign}${abs(pnl):,.0f}"


def _entry(
    label: str,
    rate: Optional[float],
    bias: Optional[str],
    confidence: Optional[float],
    spot: Optional[float],
    *,
    grade: Optional[str] = None,
    direction: str = "NO_TRADE",
    fix: dict | None = None,
    status: str = "forecast",
    note: str | None = None,
) -> dict:
    executable_forecast_mid = reanchor_to_fix(rate, spot, fix)
    pnl = net_hedge_pnl_usd(direction, executable_forecast_mid, fix)
    return {
        "horizon": label,
        "expected_rate": round(rate, 4) if rate is not None else None,
        "bias": bias or "HOLD",
        "confidence": round(float(confidence), 1) if confidence is not None else 0.0,
        "expected_move_pct": _move_pct(rate, spot),
        "grade": _display_grade(grade, pnl),
        "opportunity_grade": grade,
        "hedge_pnl_usd": pnl,
        "hedge_forecast_fix_mid": round(executable_forecast_mid, 4) if executable_forecast_mid is not None else None,
        "status": status,
        "note": note,
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


def _next_weekly_close(now: datetime) -> datetime:
    """Return the next regular Friday 21:00 UTC close after ``now``."""
    now = now.astimezone(timezone.utc)
    days_until_friday = (4 - now.weekday()) % 7
    close = (now + timedelta(days=days_until_friday)).replace(
        hour=_FX_CLOSE_HOUR_UTC, minute=0, second=0, microsecond=0
    )
    if close <= now:
        close += timedelta(days=7)
    return close


def _session_horizons(
    *,
    now: datetime,
    spot: Optional[float],
    direction: str,
    overall_grade: Optional[str],
    intraday_target: Optional[float],
    intraday_bias: str,
    intraday_conf: float,
    eod: dict,
    multiday: dict,
    fix: dict | None,
) -> tuple[list[dict], dict]:
    state = get_market_state(now=now)
    next_close = _next_weekly_close(now)
    hours_to_close = max(0.0, (next_close - now).total_seconds() / 3600.0) if state.is_open else 0.0

    def interp(frac: float) -> Optional[float]:
        if intraday_target is None or not spot:
            return None
        return spot + frac * (intraday_target - spot)

    kw = {"grade": overall_grade, "direction": direction, "fix": fix}

    if not state.is_open:
        horizons = [
            _entry(
                "Next market open",
                None,
                "HOLD",
                0.0,
                spot,
                status="market_closed",
                note=f"FX market closed; next open {state.next_market_open}.",
                **kw,
            )
        ]
        return horizons, {
            "is_open": False,
            "market_status": state.market_status,
            "hours_to_close": 0.0,
            "next_market_open": state.next_market_open,
            "intraday_truncated": True,
        }

    # Normal Sun-Thu behavior: all standard horizons are inside the current
    # continuous trading week.
    if hours_to_close >= 24.0:
        horizons = [
            _entry("1 hour", interp(0.25), intraday_bias, intraday_conf, spot, **kw),
            _entry("2 hours", interp(0.5), intraday_bias, intraday_conf, spot, **kw),
            _entry("4 hours", interp(1.0), intraday_bias, intraday_conf, spot, **kw),
            _entry(_EOD, eod.get("target"), eod.get("bias", "HOLD"), eod.get("confidence", 0.0), spot, **kw),
            _entry("24 hours", multiday.get("target"), multiday.get("bias", "HOLD"), multiday.get("confidence", 0.0), spot, **kw),
        ]
        return horizons, {
            "is_open": True,
            "market_status": state.market_status,
            "hours_to_close": round(hours_to_close, 2),
            "next_market_open": state.next_market_open,
            "intraday_truncated": False,
        }

    # Friday: only show hourly horizons that actually occur before the close.
    horizons: list[dict] = []
    for hours, frac in ((1.0, 0.25), (2.0, 0.5), (4.0, 1.0)):
        if hours <= hours_to_close + 1e-6:
            label = f"{int(hours)} hour" if hours == 1.0 else f"{int(hours)} hours"
            horizons.append(_entry(label, interp(frac), intraday_bias, intraday_conf, spot, **kw))

    # Friday's model EOD target is the only valid remaining terminal intraday
    # target, so expose it as Market close rather than pretending it is 4 hours.
    horizons.append(
        _entry(
            "Market close",
            eod.get("target") if eod.get("target") is not None else interp(min(1.0, hours_to_close / 4.0)),
            eod.get("bias", intraday_bias),
            eod.get("confidence", intraday_conf),
            spot,
            status="session_close",
            note=f"Regular FX close at {next_close.isoformat()} ({hours_to_close:.1f}h away).",
            **kw,
        )
    )

    # Do not manufacture a 24-hour weekend quote.  Tell the UI explicitly that
    # the next executable horizon is the Sunday reopen.
    horizons.append(
        _entry(
            "Next market open",
            None,
            "HOLD",
            0.0,
            spot,
            status="market_closed",
            note=f"Weekend closure follows Friday close; next open {state.next_market_open}.",
            **kw,
        )
    )
    return horizons, {
        "is_open": True,
        "market_status": state.market_status,
        "hours_to_close": round(hours_to_close, 2),
        "next_market_open": state.next_market_open,
        "intraday_truncated": True,
        "market_close": next_close.isoformat(),
    }


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

    now = datetime.now(timezone.utc)
    horizons, session = _session_horizons(
        now=now,
        spot=spot,
        direction=direction,
        overall_grade=overall_grade,
        intraday_target=intraday_target,
        intraday_bias=intraday_bias,
        intraday_conf=intraday_conf,
        eod=eod,
        multiday=multiday,
        fix=fix,
    )

    long_bailout, short_bailout = _bailouts(spot, direction, primary_stop)
    fix_bid = fix.get("bid") if fix else None
    fix_ask = fix.get("ask") if fix else None
    fix_mid = (float(fix_bid) + float(fix_ask)) / 2.0 if fix_bid is not None and fix_ask is not None else None
    entry_rate = fix_bid if direction == "SELL_USD" else fix_ask if direction == "BUY_USD" else None
    return {
        "now": round(spot, 4) if spot is not None else None,
        "opportunity_grade": overall_grade,
        "horizons": horizons,
        "session": session,
        "long_usd_bailout": long_bailout,
        "short_usd_bailout": short_bailout,
        "hedge": {
            "notional_usd": int(_HEDGE_USD),
            "fee_per_side_usd": _FEE_PER_SIDE_USD,
            "round_trip_fees_usd": 2 * _FEE_PER_SIDE_USD,
            "fix_bid": fix_bid,
            "fix_ask": fix_ask,
            "fix_mid": round(fix_mid, 5) if fix_mid is not None else None,
            "fix_spread": fix.get("spread") if fix else None,
            "entry_rate": entry_rate,
            "entry_side": "FIX bid" if direction == "SELL_USD" else "FIX ask" if direction == "BUY_USD" else None,
            "forecast_basis": "Model move re-anchored to live FIX midpoint",
            "available": entry_rate is not None,
        },
        "explanation": _explanation(direction, spot, long_bailout, short_bailout, grade=overall_grade),
    }
