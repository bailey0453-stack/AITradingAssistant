"""Evidence-based topline USD/MXN forecast (decision support only).

Numeric forecast levels come from measured historical analog moves.  A separate
empirical calibration layer then evaluates each displayed horizon against its own
paper-recommendation outcomes.  Calibration can shrink an over-extended move and
adjust confidence, but it never invents a target or expands the analog move.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from statistics import median
from typing import Optional

from app.database import SessionLocal
from app.services.fix.provider import get_fix_quote
from app.services.hedge_pnl import (
    FEE_PER_SIDE_USD as _FEE_PER_SIDE_USD,
    HEDGE_USD as _HEDGE_USD,
    net_hedge_pnl_usd,
    reanchor_to_fix,
)
from app.services.horizon_calibration import calibration_summary
from app.services.market_hours import get_market_state

_FX_CLOSE_HOUR_UTC = 21
_MIN_ANALOGS = 5
_ACTIONABLE = {"BUY_USD", "SELL_USD"}


def _move_pct(rate: Optional[float], spot: Optional[float]) -> Optional[float]:
    if rate is None or not spot:
        return None
    return round((rate / spot - 1) * 100, 3)


def _display_grade(grade: Optional[str], pnl: Optional[float]) -> Optional[str]:
    if grade is None or pnl is None:
        return grade
    sign = "+" if pnl >= 0 else "-"
    return f"{grade} · Net {sign}${abs(pnl):,.0f}"


def _entry(label, rate, bias, confidence, spot, *, grade=None, direction="NO_TRADE",
           fix=None, status="forecast", note=None, evidence=None) -> dict:
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
        "evidence": evidence,
    }


def _weighted_median(pairs: list[tuple[float, float]]) -> Optional[float]:
    clean = sorted((float(v), max(0.0, float(w))) for v, w in pairs if v is not None)
    if not clean:
        return None
    total = sum(w for _, w in clean)
    if total <= 0:
        return float(median(v for v, _ in clean))
    halfway = total / 2.0
    acc = 0.0
    for value, weight in clean:
        acc += weight
        if acc >= halfway:
            return value
    return clean[-1][0]


def _analog_move(payload: dict, window: str, direction: str) -> tuple[Optional[float], dict]:
    historical = payload.get("historical") or {}
    matches = historical.get("top_matches") or []
    pairs: list[tuple[float, float]] = []
    for match in matches:
        value = (match.get("windows") or {}).get(window)
        if value is None:
            continue
        pairs.append((float(value), float(match.get("similarity_score") or 0.0)))

    meta = {
        "method": "weighted_median_historical_analogs",
        "window": window,
        "sample_size": len(pairs),
        "minimum_sample": _MIN_ANALOGS,
        "source": historical.get("historical_source") or "unavailable",
    }
    if len(pairs) < _MIN_ANALOGS:
        meta["status"] = "insufficient_sample"
        return None, meta

    move = _weighted_median(pairs)
    if move is None:
        meta["status"] = "unavailable"
        return None, meta
    agrees = (direction == "BUY_USD" and move > 0) or (direction == "SELL_USD" and move < 0)
    if direction not in _ACTIONABLE or not agrees:
        meta.update({"status": "evidence_disagrees", "measured_move_pct": round(move, 4)})
        return None, meta

    meta.update({"status": "measured", "measured_move_pct": round(move, 4)})
    return move, meta


def _rate_from_move(spot: Optional[float], move_pct: Optional[float]) -> Optional[float]:
    if not spot or move_pct is None:
        return None
    return spot * (1.0 + move_pct / 100.0)


def _two_hour_move(one_h: Optional[float], four_h: Optional[float]) -> tuple[Optional[float], dict]:
    meta = {"method": "interpolated_between_measured_1h_and_4h", "status": "unavailable"}
    if one_h is None or four_h is None or one_h * four_h <= 0:
        return None, meta
    move = one_h + (four_h - one_h) / 3.0
    meta.update({"status": "derived", "measured_move_pct": round(move, 4)})
    return move, meta


def _evidence_bailouts(payload: dict, spot: Optional[float], direction: str) -> tuple[Optional[float], Optional[float], dict]:
    historical = payload.get("historical") or {}
    matches = historical.get("top_matches") or []
    adverse = []
    for match in matches:
        value = match.get("max_adverse_excursion")
        if value is not None:
            adverse.append(abs(float(value)))
    meta = {"method": "median_historical_adverse_excursion", "sample_size": len(adverse)}
    if not spot or direction not in _ACTIONABLE or len(adverse) < _MIN_ANALOGS:
        meta["status"] = "insufficient_sample"
        return None, None, meta
    stop_pct = median(adverse)
    if stop_pct <= 0:
        meta["status"] = "unavailable"
        return None, None, meta
    meta.update({"status": "measured", "adverse_excursion_pct": round(stop_pct, 4)})
    return round(spot * (1 - stop_pct / 100.0), 4), round(spot * (1 + stop_pct / 100.0), 4), meta


def _next_weekly_close(now: datetime) -> datetime:
    now = now.astimezone(timezone.utc)
    days_until_friday = (4 - now.weekday()) % 7
    close = (now + timedelta(days=days_until_friday)).replace(
        hour=_FX_CLOSE_HOUR_UTC, minute=0, second=0, microsecond=0
    )
    if close <= now:
        close += timedelta(days=7)
    return close


def _with_calibration(evidence: dict, calibration: dict | None) -> dict:
    out = dict(evidence or {})
    if calibration:
        out["horizon_calibration"] = calibration
    return out


def _calibrated_value(cal: dict | None, key: str, fallback):
    if not cal:
        return fallback
    value = cal.get(key)
    return fallback if value is None else value


def _calibrations(direction: str, moves: dict, confidences: dict) -> dict:
    if direction not in _ACTIONABLE:
        return {}
    db = SessionLocal()
    try:
        return calibration_summary(
            db,
            direction=direction,
            raw_moves=moves,
            raw_confidences=confidences,
        )
    except Exception:
        return {}
    finally:
        db.close()


def _explanation(direction, spot, long_bailout, short_bailout, *, grade=None, evidence_status=None) -> str:
    grade_bit = f" (Grade {grade})" if grade else ""
    if not spot:
        return "Market data unavailable — no expected rate path or bailout levels. Decision support only."
    if evidence_status != "measured":
        return (
            f"{direction} lean{grade_bit}, but a numeric target is withheld because the "
            "historical analog evidence is insufficient or disagrees. No preset move is substituted."
        )
    if direction == "BUY_USD":
        inv = f" Evidence-based long bailout {long_bailout:g}." if long_bailout else ""
        return f"Primary lean BUY_USD{grade_bit}; targets use measured analog moves with separate horizon calibration.{inv}"
    if direction == "SELL_USD":
        inv = f" Evidence-based short bailout {short_bailout:g}." if short_bailout else ""
        return f"Primary lean SELL_USD{grade_bit}; targets use measured analog moves with separate horizon calibration.{inv}"
    return f"Neutral bias{grade_bit} around spot {spot:g}; numeric directional targets are withheld."


def build(payload: dict) -> dict:
    market = payload.get("market") or {}
    spot = market.get("usdmxn")
    direction = payload.get("direction") or "NO_TRADE"
    overall_grade = payload.get("opportunity_grade")
    by_name = {h.get("horizon"): h for h in (payload.get("time_horizons") or [])}
    intraday = by_name.get("1-4 hours", {})
    eod_h = by_name.get("End of day", {})
    multi = by_name.get("1-2 days", {})

    try:
        fix = get_fix_quote()
    except Exception:
        fix = None

    one_move, one_ev = _analog_move(payload, "1h", direction)
    four_move, four_ev = _analog_move(payload, "4h", direction)
    day_move, day_ev = _analog_move(payload, "1d", direction)
    two_move, two_ev = _two_hour_move(one_move, four_move)

    raw_confidences = {
        "1h": intraday.get("confidence", 0),
        "2h": intraday.get("confidence", 0),
        "4h": intraday.get("confidence", 0),
        "end_of_day": eod_h.get("confidence", 0),
        "24h": multi.get("confidence", 0),
    }
    raw_moves = {
        "1h": one_move,
        "2h": two_move,
        "4h": four_move,
        "end_of_day": day_move,
        "24h": day_move,
    }
    cal = _calibrations(direction, raw_moves, raw_confidences)

    one_move = _calibrated_value(cal.get("1h"), "calibrated_move_pct", one_move)
    two_move = _calibrated_value(cal.get("2h"), "calibrated_move_pct", two_move)
    four_move = _calibrated_value(cal.get("4h"), "calibrated_move_pct", four_move)
    eod_move = _calibrated_value(cal.get("end_of_day"), "calibrated_move_pct", day_move)
    day24_move = _calibrated_value(cal.get("24h"), "calibrated_move_pct", day_move)

    one_conf = _calibrated_value(cal.get("1h"), "calibrated_confidence", raw_confidences["1h"])
    two_conf = _calibrated_value(cal.get("2h"), "calibrated_confidence", raw_confidences["2h"])
    four_conf = _calibrated_value(cal.get("4h"), "calibrated_confidence", raw_confidences["4h"])
    eod_conf = _calibrated_value(cal.get("end_of_day"), "calibrated_confidence", raw_confidences["end_of_day"])
    day24_conf = _calibrated_value(cal.get("24h"), "calibrated_confidence", raw_confidences["24h"])

    one_rate = _rate_from_move(spot, one_move)
    two_rate = _rate_from_move(spot, two_move)
    four_rate = _rate_from_move(spot, four_move)
    eod_rate = _rate_from_move(spot, eod_move)
    day24_rate = _rate_from_move(spot, day24_move)

    one_ev = _with_calibration(one_ev, cal.get("1h"))
    two_ev = _with_calibration(two_ev, cal.get("2h"))
    four_ev = _with_calibration(four_ev, cal.get("4h"))
    eod_ev = _with_calibration(day_ev, cal.get("end_of_day"))
    day24_ev = _with_calibration(day_ev, cal.get("24h"))

    long_bailout, short_bailout, bailout_ev = _evidence_bailouts(payload, spot, direction)
    now = datetime.now(timezone.utc)
    state = get_market_state(now=now)
    next_close = _next_weekly_close(now)
    hours_to_close = max(0.0, (next_close - now).total_seconds() / 3600.0) if state.is_open else 0.0
    kw = {"grade": overall_grade, "direction": direction, "fix": fix}

    if not state.is_open:
        horizons = [_entry("Next market open", None, "HOLD", 0.0, spot, status="market_closed",
                           note=f"FX market closed; next open {state.next_market_open}.", **kw)]
    else:
        horizons = []
        if hours_to_close >= 1:
            horizons.append(_entry("1 hour", one_rate, intraday.get("bias", "HOLD"), one_conf, spot,
                                   status="forecast" if one_rate is not None else "evidence_withheld", evidence=one_ev, **kw))
        if hours_to_close >= 2:
            horizons.append(_entry("2 hours", two_rate, intraday.get("bias", "HOLD"), two_conf, spot,
                                   status="forecast" if two_rate is not None else "evidence_withheld", evidence=two_ev, **kw))
        if hours_to_close >= 4:
            horizons.append(_entry("4 hours", four_rate, intraday.get("bias", "HOLD"), four_conf, spot,
                                   status="forecast" if four_rate is not None else "evidence_withheld", evidence=four_ev, **kw))
        close_label = "End of day" if hours_to_close >= 24 else "Market close"
        horizons.append(_entry(close_label, eod_rate, eod_h.get("bias", "HOLD"), eod_conf, spot,
                               status="forecast" if eod_rate is not None else "evidence_withheld", evidence=eod_ev, **kw))
        if hours_to_close >= 24:
            horizons.append(_entry("24 hours", day24_rate, multi.get("bias", "HOLD"), day24_conf, spot,
                                   status="forecast" if day24_rate is not None else "evidence_withheld", evidence=day24_ev, **kw))
        else:
            horizons.append(_entry("Next market open", None, "HOLD", 0.0, spot, status="market_closed",
                                   note=f"Weekend closure follows Friday close; next open {state.next_market_open}.", **kw))

    fix_bid = fix.get("bid") if fix else None
    fix_ask = fix.get("ask") if fix else None
    fix_mid = (float(fix_bid) + float(fix_ask)) / 2.0 if fix_bid is not None and fix_ask is not None else None
    entry_rate = fix_bid if direction == "SELL_USD" else fix_ask if direction == "BUY_USD" else None
    evidence_status = four_ev.get("status") if four_ev else "unavailable"
    return {
        "now": round(spot, 4) if spot is not None else None,
        "opportunity_grade": overall_grade,
        "horizons": horizons,
        "horizon_calibration": cal,
        "session": {
            "is_open": state.is_open,
            "market_status": state.market_status,
            "hours_to_close": round(hours_to_close, 2),
            "next_market_open": state.next_market_open,
            "intraday_truncated": bool(state.is_open and hours_to_close < 24),
        },
        "forecast_method": "historical_analog_weighted_median+horizon_empirical_shrinkage_v1",
        "minimum_analog_sample": _MIN_ANALOGS,
        "long_usd_bailout": long_bailout,
        "short_usd_bailout": short_bailout,
        "bailout_evidence": bailout_ev,
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
            "forecast_basis": "Measured analog moves, separately calibrated by realized outcomes for each horizon",
            "available": entry_rate is not None,
        },
        "explanation": _explanation(direction, spot, long_bailout, short_bailout,
                                    grade=overall_grade, evidence_status=evidence_status),
    }
