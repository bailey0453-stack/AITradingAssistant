"""Topline Rate Forecast (decision support only — never trade execution).

Derives a compact, top-of-dashboard view of the expected USD/MXN rate path and
bailout (thesis-invalidation) levels from the existing multi-horizon outlook
(``time_horizons``). Every projected rate and bailout level is an ESTIMATE; the
current spot carries its own provenance (live / cached / fallback / sample).

Horizon mapping (per product spec):
  - 1 hour / 2 hours  -> interpolated from the "1-4 hours" intraday horizon
  - 4 hours           -> the "1-4 hours" intraday horizon target
  - End of day        -> the "End of day" horizon
  - 24 hours          -> the "1-2 days" horizon
"""

from __future__ import annotations

from typing import Optional

# Source labels in ``time_horizons`` (see services/ai_analysis.py _HORIZON_SPECS).
_INTRADAY = "1-4 hours"
_EOD = "End of day"
_MULTIDAY = "1-2 days"

# Default bailout distance when no primary stop is available (matches the
# intraday horizon's stop sizing of 0.2%).
_DEFAULT_STOP_PCT = 0.002


def _move_pct(rate: Optional[float], spot: Optional[float]) -> Optional[float]:
    if rate is None or not spot:
        return None
    return round((rate / spot - 1) * 100, 3)


def _entry(label: str, rate: Optional[float], bias: Optional[str],
           confidence: Optional[float], spot: Optional[float]) -> dict:
    return {
        "horizon": label,
        "expected_rate": round(rate, 4) if rate is not None else None,
        "bias": bias or "HOLD",
        "confidence": round(float(confidence), 1) if confidence is not None else 0.0,
        "expected_move_pct": _move_pct(rate, spot),
    }


def _bailouts(
    spot: Optional[float], direction: str, primary_stop: Optional[float]
) -> tuple[Optional[float], Optional[float]]:
    """Return ``(long_usd_bailout, short_usd_bailout)``.

    The long bailout sits BELOW spot (a BUY_USD thesis fails as price falls);
    the short bailout sits ABOVE spot (a SELL_USD thesis fails as price rises).
    Both are ``None`` (N/A) when there is no active directional thesis.
    """
    if not spot or direction not in ("BUY_USD", "SELL_USD"):
        return None, None
    stop_pct = abs(spot - primary_stop) / spot if primary_stop else _DEFAULT_STOP_PCT
    long_bailout = round(spot * (1 - stop_pct), 4)
    short_bailout = round(spot * (1 + stop_pct), 4)
    # Anchor the primary side to the actual primary stop when present.
    if direction == "BUY_USD" and primary_stop:
        long_bailout = round(primary_stop, 4)
    elif direction == "SELL_USD" and primary_stop:
        short_bailout = round(primary_stop, 4)
    return long_bailout, short_bailout


def _explanation(
    direction: str, spot: Optional[float],
    long_bailout: Optional[float], short_bailout: Optional[float],
) -> str:
    if not spot:
        return (
            "Market data unavailable — no expected rate path or bailout levels. "
            "Decision support only."
        )
    if direction == "BUY_USD":
        return (
            f"Primary lean is BUY_USD: the expected path projects above spot {spot:g}. "
            f"The long thesis is invalidated below {long_bailout:g} (Long USD bailout); "
            f"the reverse short-thesis level is {short_bailout:g}. "
            "Estimates — decision support only, not execution."
        )
    if direction == "SELL_USD":
        return (
            f"Primary lean is SELL_USD: the expected path projects below spot {spot:g}. "
            f"The short thesis is invalidated above {short_bailout:g} (Short USD bailout); "
            f"the reverse long-thesis level is {long_bailout:g}. "
            "Estimates — decision support only, not execution."
        )
    if direction == "HOLD":
        return (
            f"Neutral HOLD bias around spot {spot:g}: expected rates are "
            "range-bound and bailout levels are N/A until conviction rises. "
            "Decision support only."
        )
    if direction == "NO_TRADE":
        return (
            f"Stand aside (NO_TRADE) around spot {spot:g}: no committed bias; "
            "bailout levels are N/A. Decision support only."
        )
    return (
        f"No directional edge around spot {spot:g}: expected rates are "
        "range-bound and bailout levels are N/A. Decision support only."
    )


def build(payload: dict) -> dict:
    """Build the ``topline_forecast`` block from a serialized analysis payload."""
    market = payload.get("market") or {}
    spot = market.get("usdmxn")
    direction = payload.get("direction") or "NO_TRADE"
    primary_stop = payload.get("stop")
    by_name = {h.get("horizon"): h for h in (payload.get("time_horizons") or [])}

    intraday = by_name.get(_INTRADAY, {})
    eod = by_name.get(_EOD, {})
    multiday = by_name.get(_MULTIDAY, {})

    intraday_target = intraday.get("target")
    intraday_bias = intraday.get("bias", "HOLD")
    intraday_conf = intraday.get("confidence", 0.0)

    def interp(frac: float) -> Optional[float]:
        # Linear path from spot (t=0) to the ~4h intraday target.
        if intraday_target is None or not spot:
            return None
        return spot + frac * (intraday_target - spot)

    horizons = [
        _entry("1 hour", interp(0.25), intraday_bias, intraday_conf, spot),
        _entry("2 hours", interp(0.5), intraday_bias, intraday_conf, spot),
        _entry("4 hours", interp(1.0), intraday_bias, intraday_conf, spot),
        _entry(_EOD, eod.get("target"), eod.get("bias", "HOLD"),
               eod.get("confidence", 0.0), spot),
        _entry("24 hours", multiday.get("target"), multiday.get("bias", "HOLD"),
               multiday.get("confidence", 0.0), spot),
    ]

    long_bailout, short_bailout = _bailouts(spot, direction, primary_stop)
    return {
        "now": round(spot, 4) if spot is not None else None,
        "horizons": horizons,
        "long_usd_bailout": long_bailout,
        "short_usd_bailout": short_bailout,
        "explanation": _explanation(direction, spot, long_bailout, short_bailout),
    }
