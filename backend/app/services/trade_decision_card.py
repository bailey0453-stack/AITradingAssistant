"""Top-of-dashboard trade decision card (decision support only).

Computes TRADE / WAIT / EXIT·INVALIDATED from explicit evidence gates.
Does **not** copy ``should_trade_now`` from decision_quality.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Optional

_ACTIONABLE = {"BUY_USD", "SELL_USD"}
_TRADE_GRADES = {"A", "A+"}
_MIN_CONFIDENCE = 70.0
_EVENT_BLOCK_WINDOW_HOURS = 4.0


def _f(v: Any) -> Optional[float]:
    try:
        return None if v is None else float(v)
    except (TypeError, ValueError):
        return None


def _direction_labels(direction: Optional[str]) -> dict:
    if direction == "SELL_USD": return {"bias":"SELL USD / BUY MXN","prediction":"USD/MXN LOWER","has_directional_forecast":True}
    if direction == "BUY_USD": return {"bias":"BUY USD / SELL MXN","prediction":"USD/MXN HIGHER","has_directional_forecast":True}
    return {"bias":"WAIT","prediction":"No sufficiently strong setup","has_directional_forecast":False}


def _forecast_agrees(direction, spot, predicted):
    if direction not in _ACTIONABLE or spot is None or predicted is None: return False
    return predicted > spot if direction == "BUY_USD" else predicted < spot


def _historical_supports(dq: dict, mp: Optional[dict]) -> bool:
    similar=(dq or {}).get("similar_track_record") or {}; avg_pnl=similar.get("similar_avg_pnl")
    if avg_pnl is None: avg_pnl=similar.get("avg_net_pnl_usd") or similar.get("avg_pnl_usd")
    avg_ret=similar.get("avg_return_pct")
    if avg_pnl is not None and float(avg_pnl)>0: return True
    if avg_ret is not None and float(avg_ret)>0: return True
    wr=similar.get("similar_win_rate")
    if wr is None: wr=similar.get("win_rate")
    if similar.get("enough_history") and wr is not None: return float(wr)>50.0
    if mp:
        exp=(mp.get("expectancy") or {}).get("avg_net_pnl_usd")
        if exp is not None and float(exp)>0: return True
    return False


def _parse_event_time(value):
    if not value: return None
    try: when=datetime.fromisoformat(str(value).replace("Z","+00:00"))
    except (TypeError,ValueError): return None
    if when.tzinfo is None: when=when.replace(tzinfo=timezone.utc)
    return when.astimezone(timezone.utc)


def _event_name(ev): return str(ev.get("event") or ev.get("title") or ev.get("name") or "High-impact event")


def _blocking_event(payload):
    ctx=payload.get("context") or {}; upcoming=ctx.get("upcoming_events") or []; now=datetime.now(timezone.utc); candidates=[]; malformed=[]
    for ev in upcoming:
        impact=str(ev.get("impact") or ev.get("importance") or "").lower()
        if impact not in ("high","3","red"): continue
        status=str(ev.get("status") or "upcoming").lower()
        if status in {"released","past","completed"}: continue
        when=_parse_event_time(ev.get("release_time") or ev.get("time") or ev.get("datetime"))
        if when is None: malformed.append(ev); continue
        hours=(when-now).total_seconds()/3600.0
        if 0 <= hours <= _EVENT_BLOCK_WINDOW_HOURS:
            candidates.append({"name":_event_name(ev),"release_time":when.isoformat(),"hours_away":round(hours,2),"importance":"high"})
    if candidates: return min(candidates,key=lambda item:item["hours_away"])
    if malformed:
        ev=malformed[0]; return {"name":_event_name(ev),"release_time":ev.get("release_time") or ev.get("time"),"hours_away":None,"importance":"high"}
    return None


def _format_event_wait(event):
    name=event.get("name") or "High-impact event"; hours=event.get("hours_away")
    if hours is None: return f"{name} is approaching (time unavailable)"
    minutes=max(0,int(round(float(hours)*60)))
    if minutes<60: return f"{name} in {minutes} min"
    h,m=divmod(minutes,60); return f"{name} in {h}h" if m==0 else f"{name} in {h}h {m}m"


def _data_fresh_enough(payload):
    market=payload.get("market") or {}; source=(market.get("source") or "").lower()
    if source in ("mock","sample"): return False
    ms=payload.get("market_state") or {}
    if ms.get("is_stale") is True: return False
    if ms.get("cached") is True and not ms.get("is_open"):
        age=ms.get("age_minutes")
        if age is not None and float(age)>24*60: return False
    prov=payload.get("provenance") or {}; freshness=(prov.get("freshness") or {}).get("market") or prov.get("market_freshness")
    if isinstance(freshness,str) and freshness.lower() in ("stale","unavailable","degraded"): return False
    return market.get("usdmxn") is not None or market.get("rate") is not None


def _invalidated(direction,spot,long_bailout,short_bailout,*,explicit=False):
    if explicit: return True
    if direction=="BUY_USD" and spot is not None and long_bailout is not None: return spot<long_bailout
    if direction=="SELL_USD" and spot is not None and short_bailout is not None: return spot>short_bailout
    return False


def _why_wait(reasons):
    if not reasons: return "Directional prediction exists, but evidence is not strong enough to trade."
    return "; ".join(reasons[:4]).rstrip(".")+"."


def build_trade_decision_card(payload: dict) -> dict:
    direction=payload.get("direction"); grade=payload.get("opportunity_grade") or payload.get("grade"); confidence=_f(payload.get("confidence")) or 0.0
    market=payload.get("market") or {}; spot=_f(market.get("usdmxn") if market.get("usdmxn") is not None else market.get("rate")); dq=payload.get("decision_quality") or {}; mp=payload.get("model_performance"); tl=payload.get("topline_forecast") or {}
    labels=_direction_labels(direction if direction in _ACTIONABLE else None)
    if direction in _ACTIONABLE: labels=_direction_labels(direction)

    rate_4h=None; rate_eod=None; rate_close=None
    for entry in tl.get("path") or tl.get("horizons") or []:
        h=(entry.get("horizon") or "").lower(); val=_f(entry.get("expected_rate"))
        if h in ("4 hours","4h","1-4 hours") and rate_4h is None: rate_4h=val
        if h in ("end of day","eod") and rate_eod is None: rate_eod=val
        if h in ("market close","session close","friday close") and rate_close is None: rate_close=val
    if rate_4h is None: rate_4h=_f((tl.get("four_hours") or {}).get("expected_rate") if isinstance(tl.get("four_hours"),dict) else tl.get("four_hours"))
    if rate_eod is None: rate_eod=_f((tl.get("end_of_day") or {}).get("expected_rate") if isinstance(tl.get("end_of_day"),dict) else tl.get("end_of_day"))

    long_bailout=_f(tl.get("long_usd_bailout")); short_bailout=_f(tl.get("short_usd_bailout")); stop=_f(payload.get("stop"))
    if direction=="BUY_USD" and long_bailout is None: long_bailout=stop
    if direction=="SELL_USD" and short_bailout is None: short_bailout=stop
    invalidation_level=None; invalidation_label="INVALIDATION"
    if direction=="BUY_USD": invalidation_level=long_bailout; invalidation_label="INVALID BELOW"
    elif direction=="SELL_USD": invalidation_level=short_bailout; invalidation_label="INVALID ABOVE"

    # Near Friday close there is deliberately no artificial 4-hour quote. Use
    # the actual session-close forecast as the primary directional check.
    predicted_primary=rate_4h if rate_4h is not None else rate_close if rate_close is not None else rate_eod
    forecast_ok=_forecast_agrees(direction,spot,predicted_primary)
    if not forecast_ok and rate_close is not None: forecast_ok=_forecast_agrees(direction,spot,rate_close)
    if not forecast_ok and rate_eod is not None: forecast_ok=_forecast_agrees(direction,spot,rate_eod)

    ev=_f((dq.get("expected_value") or {}).get("expected_value_usd"))
    if ev is None: ev=_f(dq.get("expected_value_usd"))
    explicit_invalid=bool(payload.get("invalidated") or (payload.get("status") or "").upper()=="INVALIDATED" or (dq.get("status") or "").upper()=="INVALIDATED")
    is_invalid=_invalidated(direction,spot,long_bailout,short_bailout,explicit=explicit_invalid)
    wait_reasons=[]; actionable=direction in _ACTIONABLE; grade_ok=(grade or "") in _TRADE_GRADES; conf_ok=confidence>=_MIN_CONFIDENCE; ev_ok=ev is not None and ev>0; hist_ok=_historical_supports(dq,mp); blocking_event=_blocking_event(payload); event_near=blocking_event is not None; fresh_ok=_data_fresh_enough(payload)
    if not actionable: wait_reasons.append("No actionable BUY_USD/SELL_USD signal")
    if actionable and not grade_ok: wait_reasons.append(f"Grade {grade or 'n/a'} (need A or A+)")
    if actionable and not conf_ok: wait_reasons.append(f"Confidence {confidence:.0f} below 70")
    if actionable and not ev_ok: wait_reasons.append("Negative expected value" if ev is not None and ev<=0 else "Expected value unavailable or not positive")
    if actionable and not hist_ok: wait_reasons.append("Historical comparable setups do not support acting")
    if blocking_event: wait_reasons.append(_format_event_wait(blocking_event))
    if not fresh_ok: wait_reasons.append("Required market inputs are stale or insufficient")
    if actionable and not forecast_ok: wait_reasons.append("Primary signal and forecast direction disagree")

    if is_invalid and actionable:
        action="EXIT / INVALIDATED"; visual="red"; why=f"Spot {spot:g} crossed the invalidation level ({invalidation_label} {invalidation_level:g})." if spot is not None and invalidation_level is not None else "Recommendation is invalidated."
    elif actionable and grade_ok and conf_ok and ev_ok and hist_ok and not event_near and fresh_ok and forecast_ok and not is_invalid:
        action="TRADE"; visual="green"; why=f"Grade {grade}, confidence {confidence:.0f}, positive expected value, and historical support align with the forecast."
    else:
        action="WAIT"; visual="yellow"; why=_why_wait(wait_reasons)

    return {"action":action,"visual":visual,"direction":direction,"bias":labels["bias"],"prediction":labels["prediction"],"has_directional_forecast":labels["has_directional_forecast"] or actionable,"spot":round(spot,4) if spot is not None else None,"predicted_4h":round(rate_4h,4) if rate_4h is not None else None,"predicted_market_close":round(rate_close,4) if rate_close is not None else None,"predicted_eod":round(rate_eod,4) if rate_eod is not None else None,"invalidation_label":invalidation_label,"invalidation_level":round(invalidation_level,4) if invalidation_level is not None else None,"why":why,"wait_reasons":wait_reasons,"blocking_event":blocking_event,"event_block_window_hours":_EVENT_BLOCK_WINDOW_HOURS,"gates":{"actionable":actionable,"grade_ok":grade_ok,"confidence_ok":conf_ok,"expected_value_ok":ev_ok,"historical_support":hist_ok,"no_event_risk":not event_near,"data_fresh":fresh_ok,"forecast_agrees":forecast_ok,"not_invalidated":not is_invalid},"inputs":{"grade":grade,"confidence":confidence,"expected_value_usd":ev,"should_trade_now_ignored":dq.get("should_trade_now")},"disclaimer":"Decision support only — not an order or automated trade."}
