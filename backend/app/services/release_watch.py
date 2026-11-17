"""Before/after economic-release workflow with frozen consensus and live data.

Only a numeric actual for an identified release is a result. Headlines, previous
values, provider model forecasts and post-release consensus cannot substitute.
Price targets require genuine measured 20-minute outcomes for the same series.
"""
from __future__ import annotations

import hashlib
import math
import re
from datetime import datetime, timedelta, timezone
from statistics import mean
from threading import Lock
from time import monotonic

import httpx
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.config import get_settings
from app.models.release_watch import ReleaseWatch
from app.services.hedge_pnl import hedge_pnl_detail

_lock = Lock()
_cache = None
MIN_EVENTS = 10


def aware(value):
    try:
        value = value if isinstance(value, datetime) else datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)
    except (TypeError, ValueError):
        return None


def number(value):
    if value is None or isinstance(value, bool):
        return None
    m = re.fullmatch(r"\s*([+−-]?(?:\d[\d,]*\.?\d*|\.\d+))\s*([KMB%]?)\s*", str(value), re.I)
    if not m:
        return None
    result = float(m[1].replace(",", "").replace("−", "-")) * {"K":1e3,"M":1e6,"B":1e9}.get(m[2].upper(), 1)
    return result if math.isfinite(result) else None


def identify(name, country, unit):
    """Keep MoM, YoY, headline, core, ex-auto and control series distinct."""
    name = " ".join(str(name).lower().split())
    country = str(country).upper()
    if country not in {"US", "MX"}:
        return None
    rate = bool(re.search(r"rate decision|interest rate decision", name))
    if rate:
        return f"{country.lower()}_policy_rate", 1 if country == "US" else -1, "%"
    if "retail sales" in name and country == "US":
        if not re.search(r"mom|m-o-m|month.over.month", name) and "control" not in name:
            return None
        suffix = "control" if "control" in name else "ex_auto_gas" if re.search(r"gas.*auto|auto.*gas", name) else "ex_auto" if re.search(r"ex.*auto", name) else "headline"
        return f"us_retail_mom_{suffix}", 1, "%"
    if country == "US" and re.search(r"non.?farm payroll", name):
        return "us_nonfarm_payrolls", 1, "count"
    if country == "US" and "unemployment rate" in name:
        return "us_unemployment_rate", -1, "%"
    if re.search(r"cpi|consumer price|inflation rate", name):
        period = "mom" if re.search(r"mom|m-o-m|month.over.month", name) else "yoy" if re.search(r"yoy|y-o-y|year.over.year", name) else None
        if period:
            return f"{country.lower()}_cpi_{'core' if 'core' in name else 'headline'}_{period}", 1 if country == "US" else -1, "%"
    if country == "US" and re.search(r"industrial production|import prices?|export prices?", name) and re.search(r"mom|m-o-m|month.over.month", name):
        series = "industrial_production" if "industrial" in name else "import_prices" if "import" in name else "export_prices"
        return f"us_{series}_mom", 1, "%"
    return None


def _map(row, provider):
    if provider == "finnhub":
        country = {"UNITED STATES":"US","USA":"US","MEXICO":"MX"}.get(str(row.get("country") or "").upper(),str(row.get("country") or "").upper())
        name, date, forecast, actual, previous = row.get("event"), row.get("time"), row.get("estimate"), row.get("actual"), row.get("prev")
        unit, reference, provider_id = row.get("unit") or "", row.get("reference") or "", row.get("id")
        precise = True
    else:
        country = {"united states":"US","mexico":"MX"}.get(str(row.get("Country") or "").lower(), "")
        name, date, forecast, actual, previous = row.get("Event"), row.get("Date"), row.get("Forecast"), row.get("Actual"), row.get("Previous")
        unit, reference, provider_id = row.get("Unit") or "", row.get("Reference") or "", row.get("CalendarID")
        precise = str(row.get("DateSpan", "0")) == "0"
    spec, at = identify(name, country, unit), aware(date)
    if not spec or not at or not precise:
        return None
    series, polarity, expected_unit = spec
    normalized_unit = str(unit).strip().lower()
    if expected_unit == "%" and normalized_unit not in {"%", "percent", "percentage", ""}:
        return None
    def parsed(value):
        result=number(value)
        if result is not None and expected_unit=="count" and normalized_unit in {"k","m","b"} and not re.search(r"[kmb]\s*$",str(value),re.I):
            result *= {"k":1e3,"m":1e6,"b":1e9}[normalized_unit]
        return result
    key = hashlib.sha256(f"{provider}|{provider_id or series}|{series}|{at.isoformat()}|{reference}".encode()).hexdigest()
    return {"event_key":key,"event_name":str(name),"series":series,"country":country,
            "unit":expected_unit,"reference":str(reference),"release_time":at,
            "consensus":parsed(forecast),"actual":parsed(actual),"previous":parsed(previous),
            "source":provider,"polarity":polarity}


def feed(*, settings=None, now=None):
    """Use explicit calendar credentials or probe existing Finnhub entitlement.

    No plan purchase or automatic upgrade. Errors do not fall back to mock data.
    Credentials remain in headers, never URLs or returned error messages.
    """
    global _cache
    settings = settings or get_settings()
    now = aware(now or datetime.now(timezone.utc))
    provider = settings.calendar_provider.lower()
    token = settings.calendar_api_key
    if not token and settings.news_provider.lower() == "finnhub":
        provider, token = "finnhub", settings.news_api_key
    key = provider, hash(token), settings.is_mock
    with _lock:
        if _cache and _cache[0] == key and monotonic()-_cache[1] < _cache[2]:
            return _cache[3]
        result = {"status":"UNAVAILABLE", "provider":provider, "checked_at":now.isoformat(), "events":[], "error":None}
        ttl = 300
        if settings.is_mock or not token or provider not in {"finnhub", "tradingeconomics"}:
            result["error"] = "Structured actual/consensus data access is not configured. The existing official calendar supplies dates only."
        else:
            try:
                start, end = (now-timedelta(days=1)).date().isoformat(), (now+timedelta(days=3)).date().isoformat()
                if provider == "finnhub":
                    response = httpx.get("https://finnhub.io/api/v1/calendar/economic", params={"from":start,"to":end}, headers={"X-Finnhub-Token":token}, timeout=4)
                else:
                    response = httpx.get("https://api.tradingeconomics.com/calendar/country/united states,mexico", params={"d1":start,"d2":end,"f":"json"}, headers={"Authorization":token}, timeout=4)
                if response.status_code in {401,403}:
                    result["status"] = "ACCESS_REQUIRED"
                    result["error"] = "The current data account did not grant structured economic-calendar access. No subscription was changed."
                    ttl = 3600
                else:
                    response.raise_for_status()
                    data = response.json()
                    rows = data.get("economicCalendar") if provider == "finnhub" and isinstance(data, dict) else data
                    if not isinstance(rows, list):
                        raise ValueError("Unexpected release payload")
                    result["events"] = [event for row in rows if isinstance(row,dict) and (event := _map(row,provider))]
                    result["status"] = "LIVE_POLL"
                    near = any(abs((e["release_time"]-now).total_seconds()) <= 25*60 for e in result["events"])
                    ttl = 5 if near else 300
            except Exception:
                result["error"] = "The structured release feed could not be checked; no released numbers were inferred from headlines."
                ttl = 30
        result["poll_seconds"] = ttl
        result["delivery_latency"] = "unverified"
        _cache = key, monotonic(), ttl, result
        return result


def usable_fix(fix, now):
    fix = fix or {}
    bid, ask, at = number(fix.get("bid")), number(fix.get("ask")), aware(fix.get("updated_at"))
    if bid is None or ask is None or bid <= 0 or ask < bid or not at or not -2 <= (now-at).total_seconds() <= 30:
        return None
    return bid, ask, at


def capture(db, data, fix, *, now=None):
    """Freeze only expectations actually observed before the release."""
    now = aware(now or datetime.now(timezone.utc))
    if data.get("status") != "LIVE_POLL":
        return []
    price = usable_fix(fix, now)
    rows = []
    for event in data.get("events", []):
        if not now-timedelta(hours=1) <= event["release_time"] <= now+timedelta(days=3):
            continue
        row = db.execute(select(ReleaseWatch).where(ReleaseWatch.event_key == event["event_key"])).scalar_one_or_none()
        if row is None:
            row = ReleaseWatch(**{k:event[k] for k in ("event_key","event_name","series","country","unit","reference","release_time","source")})
            db.add(row)
        row.last_seen_at = now
        if now < event["release_time"] and event["actual"] is None:
            # Update to the last genuine pre-release consensus. Never overwrite
            # it after the result is published, even if a vendor revises history.
            if event["consensus"] is not None:
                row.consensus, row.consensus_seen_at = event["consensus"], now
            row.previous = event["previous"]
            if price and price[2] < event["release_time"] and (event["release_time"]-price[2]).total_seconds() <= 60:
                row.pre_bid, row.pre_ask, row.pre_quote_at = price
        elif now>=event["release_time"] and event["actual"] is not None and aware(data.get("checked_at")) and aware(data["checked_at"])>=event["release_time"]:
            if row.actual is None:
                row.actual, row.actual_seen_at = event["actual"], now
            row.latest_actual, row.revised_previous = event["actual"], event["previous"]
        if price and row.pre_bid and row.pre_ask and row.outcome_20m is None:
            target_at = event["release_time"]+timedelta(minutes=20)
            if price[2]<=now and abs((price[2]-target_at).total_seconds()) <= 30:
                row.outcome_20m = ((price[0]+price[1])/(row.pre_bid+row.pre_ask)-1)
                row.outcome_at = price[2]
        rows.append(row)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        # A concurrent watcher won the insert; re-read without rewriting times.
        return list(db.execute(select(ReleaseWatch).where(ReleaseWatch.event_key.in_([e["event_key"] for e in data.get("events",[])]))).scalars())
    return rows


def _outcomes(cluster, history, release_at):
    """One sample per release time, matching the complete surprise combination."""
    groups={}
    for old in history:
        if aware(old.release_time)<release_at:
            groups.setdefault(aware(old.release_time),{})[old.series]=old
    matches=[]
    for at,group in groups.items():
        valid=[]
        for current in cluster:
            old=group.get(current.series)
            if old is None or old.unit!=current.unit or old.outcome_20m is None or not old.outcome_at or aware(old.outcome_at)>=release_at:
                break
            if old.source not in {"finnhub","tradingeconomics"} or not old.pre_quote_at or not 0<=(at-aware(old.pre_quote_at)).total_seconds()<=60 or abs((aware(old.outcome_at)-at-timedelta(minutes=20)).total_seconds())>30:
                break
            if old.consensus is None or old.actual is None or not old.consensus_seen_at or aware(old.consensus_seen_at)>=at or not old.actual_seen_at or not 0 <= (aware(old.actual_seen_at)-at).total_seconds()<=60:
                break
            if old.latest_actual!=old.actual or (old.previous is not None and old.revised_previous is not None and old.previous!=old.revised_previous):
                break
            current_surprise=current.actual-current.consensus
            old_surprise=old.actual-old.consensus
            sign=lambda value: 1 if value>0 else -1 if value<0 else 0
            if sign(old_surprise)!=sign(current_surprise):
                break
            valid.append(old.outcome_20m)
        else:
            if valid:
                matches.append(mean(valid))
    return matches


def assess(rows, data, fix, *, history=(), now=None):
    now = aware(now or datetime.now(timezone.utc))
    out = {"action":"WAIT","phase":"NO_RELEASE", "in_control":False,
           "feed_status":data.get("status"),"provider":data.get("provider"),"checked_at":data.get("checked_at"),
           "error":data.get("error"),"poll_seconds":data.get("poll_seconds",300),"release_time":None,
           "events":[],"scenarios":[],"expected_rate":None,"expected_move_mxn":None,"net_pnl_usd":None,
           "confidence":None,"confidence_label":"Not calibrated", "wait_reasons":[],"history_samples":0,
           "delivery_latency":"Provider delivery speed is unverified."}
    if not rows:
        out["phase"] = "FEED_REQUIRED" if data.get("status") != "LIVE_POLL" else "NO_RELEASE"
        return out
    recent = [r for r in rows if aware(r.release_time) <= now < aware(r.release_time)+timedelta(minutes=20)]
    candidates = recent or [r for r in rows if aware(r.release_time)>now]
    if not candidates:
        return out
    selected = max(recent,key=lambda r:aware(r.release_time)) if recent else min(candidates,key=lambda r:aware(r.release_time))
    release_at = aware(selected.release_time)
    cluster = [r for r in rows if abs((aware(r.release_time)-release_at).total_seconds()) <= 30]
    out.update(release_time=release_at.isoformat(), forecast_for=(release_at+timedelta(minutes=20)).isoformat(),
               seconds_until=round((release_at-now).total_seconds()),in_control=abs((release_at-now).total_seconds())<=20*60)
    expected_directions = []
    for row in cluster:
        polarity = identify(row.event_name,row.country,row.unit)[1]
        frozen = row.consensus is not None and row.consensus_seen_at and aware(row.consensus_seen_at)<release_at
        published_actual=row.actual if now>=release_at else None
        surprise = published_actual-row.consensus if published_actual is not None and frozen else None
        direction = ("BUY_USD" if surprise*polarity>0 else "SELL_USD") if surprise else None
        if direction: expected_directions.append(direction)
        out["events"].append({"event":row.event_name,"series":row.series,"unit":row.unit,
            "consensus":row.consensus if frozen else None,"consensus_frozen_at":aware(row.consensus_seen_at).isoformat() if frozen else None,
            "previous":row.previous,"revised_previous":row.revised_previous if now>=release_at else None,"actual":published_actual,
            "actual_seen_at":aware(row.actual_seen_at).isoformat() if row.actual_seen_at and now>=release_at else None,
            "surprise":surprise,"surprise_direction":direction,"source":row.source})
        out["scenarios"].append({"event":row.event_name,
            "above_consensus":"USD/MXN upward bias" if polarity>0 else "USD/MXN downward bias",
            "below_consensus":"USD/MXN downward bias" if polarity>0 else "USD/MXN upward bias",
            "at_consensus":"No release-surprise edge; reassess revisions and price reaction."})
    if now<release_at:
        out["phase"]="PRE_RELEASE"
        out["wait_reasons"].append("Scenarios describe possible reactions; no result or trade call exists before publication.")
        return out
    out["phase"]="RESULT_RECEIVED" if any(e["actual"] is not None for e in out["events"]) else "AWAITING_RESULT"
    if data.get("status")!="LIVE_POLL" or not aware(data.get("checked_at")) or not -2 <= (now-aware(data["checked_at"])).total_seconds() <= 15:
        out["wait_reasons"].append("The release feed is stale or unavailable.")
    if any(e["actual"] is None for e in out["events"]):
        out["wait_reasons"].append("Waiting for all tracked simultaneous release figures.")
    if any(e["consensus"] is None for e in out["events"]):
        out["wait_reasons"].append("Consensus was not captured before this release; a later forecast cannot substitute.")
    if len(set(expected_directions)) != 1:
        out["wait_reasons"].append("The simultaneous releases conflict or show no clear surprise.")
    if any(r.actual_seen_at and not 0 <= (aware(r.actual_seen_at)-release_at).total_seconds() <= 60 for r in cluster):
        out["wait_reasons"].append("The actual figures arrived more than 60 seconds after release; immediate-reaction timing is unverified.")
    if any(r.latest_actual!=r.actual or (r.previous is not None and r.revised_previous is not None and r.previous!=r.revised_previous) for r in cluster):
        out["wait_reasons"].append("Reported revisions require reassessment before using the initial surprise.")
    price = usable_fix(fix,now)
    if not price or not selected.pre_bid or not selected.pre_quote_at or (release_at-aware(selected.pre_quote_at)).total_seconds()>60:
        out["wait_reasons"].append("Fresh executable prices and a genuine pre-release baseline are required.")
    if out["wait_reasons"]:
        return out
    direction=expected_directions[0]
    out["surprise_direction"]=direction
    mid=(price[0]+price[1])/2
    out["spot"]=round(mid,5)
    baseline=(selected.pre_bid+selected.pre_ask)/2
    observed=mid-baseline
    out["observed_move_mxn"]=round(observed,5)
    if price[2]<release_at+timedelta(seconds=10) or abs(observed)<max(price[1]-price[0],mid*.00005) or (observed>0)!=(direction=="BUY_USD"):
        out["wait_reasons"].append("The live price reaction does not yet confirm the release surprise.")
        return out
    # A monthly headline release alone cannot justify a 20-minute magnitude.
    # Use only completed genuine outcomes observed before this release.
    matches=_outcomes(cluster,history,release_at)
    out["history_samples"]=len(matches)
    out["phase"]="REACTION_CONFIRMED"
    if len(matches)<MIN_EVENTS:
        out["wait_reasons"].append(f"Price confirms the surprise, but only {len(matches)} genuine outcomes match this report/surprise combination; need {MIN_EVENTS} for a numeric estimate.")
        return out
    expected=baseline*(1+mean(matches))
    pnl=hedge_pnl_detail(direction,expected,{"bid":price[0],"ask":price[1]})
    wins=sum((r>0)==(direction=="BUY_USD") and r!=0 for r in matches)
    n=len(matches)
    p,z=wins/n,1.96
    lower=(p+z*z/(2*n)-z*math.sqrt(p*(1-p)/n+z*z/(4*n*n)))/(1+z*z/n)
    out.update(expected_rate=round(expected,5),expected_move_mxn=round(expected-mid,5),net_pnl_usd=pnl["net_pnl_usd"],
               historical_directional_hit_rate=round(p*100,1),historical_hit_rate_lower_95=round(lower*100,1),
               confidence_label="Experimental same-series estimate; live forecast accuracy is not calibrated")
    if lower<=.5:
        out["wait_reasons"].append("Same-series outcomes do not establish directional support above chance.")
    elif (expected>mid)!=(direction=="BUY_USD") or pnl["net_pnl_usd"]<=0:
        out["wait_reasons"].append("The remaining estimated move does not support this direction after FIX spread and fees.")
    else:
        out.update(action="BUY USD / SELL MXN NOW" if direction=="BUY_USD" else "SELL USD / BUY MXN NOW",phase="EXPERIMENTAL_SIGNAL")
    return out


def current(db, *, scheduled=()):
    from app.services.fix.provider import get_fix_quote
    data=feed()
    fix=get_fix_quote()
    rows=capture(db,data,fix)
    if not rows:
        # Retain the confirmed official schedule even when consensus/result
        # entitlement is missing. These rows never invent forecast or actual.
        events=[]
        for event in scheduled:
            name=event.get("event") or ""
            if name == "US Retail Sales":
                name="US Retail Sales (MoM)"
            mapped=_map({"event":name,"time":event.get("release_time"),
                         "country":event.get("country"),"unit":"%"}, "finnhub")
            if mapped:
                mapped["source"]="official_schedule"
                mapped["event_key"]=hashlib.sha256(f"schedule|{mapped['series']}|{mapped['release_time'].isoformat()}".encode()).hexdigest()
                events.append(mapped)
        rows=capture(db,{"status":"LIVE_POLL","events":events},fix)
    series={row.series for row in rows}
    history=list(db.execute(select(ReleaseWatch).where(ReleaseWatch.outcome_20m.is_not(None),ReleaseWatch.series.in_(series)).order_by(ReleaseWatch.release_time.desc()).limit(300)).scalars()) if series else []
    result=assess(rows,data,fix,history=history)
    if result["action"]!="WAIT":
        from app.services.market_hours import get_market_state
        from app.services.short_term_news import snapshot, _interpret
        now=datetime.now(timezone.utc)
        news=snapshot(db)
        blocked=[]
        if not get_market_state().is_open:
            blocked.append("The FX market is closed.")
        if news.get("status")!="LIVE_POLL":
            blocked.append("Breaking-news coverage is unavailable.")
        for item in news.get("items",[]):
            published=aware(item.get("published_at"))
            if not published or not timedelta(0)<=now-published<=timedelta(minutes=20):
                continue
            title=str(item.get("headline") or "")
            direction,shock=_interpret(title)
            own_report=any(("retail" in e["series"] and "retail sales" in title.lower()) or ("cpi" in e["series"] and re.search(r"\bcpi\b|consumer prices?",title,re.I)) for e in result["events"])
            if re.search(r"\bwar|attack|tariffs?|sanctions|missiles?\b",title,re.I) or (shock and not own_report and (not direction or direction!=result.get("surprise_direction"))):
                blocked.append("A separate fresh headline conflicts with or obscures the release assessment.")
                break
        if blocked:
            result["action"]="WAIT"
            result["wait_reasons"].extend(blocked)
    return result
