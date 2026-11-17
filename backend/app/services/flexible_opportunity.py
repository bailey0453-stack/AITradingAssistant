"""Five-cent first-touch opportunities over up to five hours, not endpoint guesses.

All statistics are sampled historical simulations. No live hit probability or
confidence is asserted. Sparse history cannot establish barrier order.
"""
from bisect import bisect_left
from datetime import datetime, timedelta, timezone
from math import isfinite, sqrt
from statistics import mean, median, pstdev, stdev

from app.services.fx_observations import aware, usable_quote
from app.services.short_term_news import assess as assess_news

DISTANCE = .05
MAX_HOLD = timedelta(hours=5)
LOOKBACK = timedelta(hours=1)
MIN_ANALOGS = 30
MIN_VALIDATION = 30


def _near(points, times, at):
    i = bisect_left(times, at)
    choices = [points[j] for j in (i-1, i) if 0 <= j < len(points)]
    row = min(choices, key=lambda p: abs(p[0]-at), default=None)
    return row if row and abs(row[0]-at) <= timedelta(minutes=2) else None


def features(points, times, at, spot):
    grid = [_near(points, times, at-timedelta(minutes=m)) for m in range(5, 61, 5)]
    if any(p is None or p[0] >= at for p in grid):
        return None
    prices = [spot] + [p[1] for p in grid]
    return (*(10_000*(spot/prices[m//5]-1) for m in (5,15,60)),
            pstdev(10_000*(a/b-1) for a,b in zip(prices,prices[1:])))


def windows(points):
    times = [p[0] for p in points]
    result, last_end = [], None
    for i, (at, spot) in enumerate(points):
        if last_end and at-LOOKBACK <= last_end:
            continue
        end_i = bisect_left(times, at+MAX_HOLD)
        if end_i >= len(points):
            break
        if times[end_i]-at-MAX_HOLD > timedelta(minutes=2):
            continue
        f = features(points, times, at, spot)
        if f is None:
            continue
        begin = bisect_left(times, at-LOOKBACK)
        if any(times[j+1]-times[j] > timedelta(minutes=2) for j in range(begin, end_i)):
            continue
        result.append({"at":at,"end":times[end_i],"features":f,
                       "path":[((t-at).total_seconds()/60, p-spot) for t,p in points[i+1:end_i+1]]})
        last_end = times[end_i]
    return result


def neighbors(rows, f):
    scored = []
    for row in rows:
        distance = sum(((a-b)/s)**2 for a,b,s in zip(row["features"],f,(3,6,15,2)))
        if distance <= 4:
            scored.append((distance, row))
    return [r for _,r in sorted(scored,key=lambda pair:pair[0])[:60]]


def net_pnl(direction, entry, exit_rate):
    if direction == "BUY_USD":
        gross = 100_000*(1-entry/exit_rate)
    else:
        gross = 100_000*(entry/exit_rate-1)
    return round(gross-40, 2)


def outcome(row, direction, fix):
    """Sampled first barrier, with stop overshoot and current constant spread."""
    bid, ask = fix["bid"], fix["ask"]
    sign = 1 if direction == "BUY_USD" else -1
    entry, exit_start = (ask,bid) if sign == 1 else (bid,ask)
    target, stop = entry+sign*DISTANCE, entry-sign*DISTANCE
    for minutes, delta in row["path"]:
        exit_rate = exit_start+delta
        if exit_rate <= 0:
            return {"kind":"unavailable","minutes":minutes,"net":None}
        if sign*(exit_rate-target) >= -1e-9:
            return {"kind":"target","minutes":minutes,"net":net_pnl(direction,entry,target)}
        if sign*(exit_rate-stop) <= 1e-9:
            return {"kind":"stop","minutes":minutes,"net":net_pnl(direction,entry,exit_rate)}
    minutes, delta = row["path"][-1]
    return {"kind":"timeout","minutes":minutes,"net":net_pnl(direction,entry,exit_start+delta)}


def summarize(results):
    results = [r for r in results if r["net"] is not None]
    n = len(results)
    profits = [r["net"] for r in results]
    hits = [r["minutes"] for r in results if r["kind"] == "target"]
    p,z = (len(hits)/n if n else 0),1.96
    lower = (p+z*z/(2*n)-z*sqrt(p*(1-p)/n+z*z/(4*n*n)))/(1+z*z/n) if n else None
    return {
        "samples":n, "target_first_pct":round(100*len(hits)/n,1) if n else None,
        "target_first_lower_95_pct":round(100*lower,1) if n else None,
        "stop_first_pct":round(100*sum(r["kind"]=="stop" for r in results)/n,1) if n else None,
        "timeout_pct":round(100*sum(r["kind"]=="timeout" for r in results)/n,1) if n else None,
        "average_net_usd":round(mean(profits),2) if n else None,
        "average_net_lower_99_usd":round(mean(profits)-2.576*stdev(profits)/sqrt(n),2) if n>1 else None,
        "target_hit_minutes_median":round(median(hits),1) if len(hits)>=10 else None,
        "label":"Historical sampled simulation; not live prediction accuracy",
    }


def choose(rows, fix):
    options = []
    for direction in ("BUY_USD","SELL_USD"):
        stats = summarize([outcome(row,direction,fix) for row in rows])
        options.append((stats["average_net_usd"] or 0, direction, stats))
    options.sort(key=lambda item:item[0],reverse=True)
    return options[0][1:] if options[0][0] > 0 else (None, options[0][2])


def validate(rows, direction, fix):
    results = []
    for i in range(max(MIN_ANALOGS,len(rows)-120),len(rows)):
        row = rows[i]
        training = [r for r in rows[:i] if r["end"] < row["at"]-LOOKBACK]
        matches = neighbors(training,row["features"])
        if len(matches) < MIN_ANALOGS:
            continue
        selected,_ = choose(matches,fix)
        if selected == direction:
            results.append(outcome(row,direction,fix))
    return summarize(results)


def upcoming_risk(payload, now):
    risks = []
    for event in (payload.get("context") or {}).get("upcoming_events",[]):
        at = aware(event.get("release_time") or event.get("time") or event.get("date"))
        impact = str(event.get("importance") or event.get("impact") or "").lower()
        if at and timedelta(0) <= at-now <= timedelta(minutes=15) and impact in {"high","3"}:
            risks.append(event.get("title") or event.get("event") or "High-impact report")
    return risks


def build(points, fix, payload, *, now=None):
    now = aware(now or datetime.now(timezone.utc))
    out = {"action":"WAIT","direction":None,"generated_at":now.isoformat(),
           "expires_at":(now+timedelta(seconds=45)).isoformat(),"max_hold_minutes":300,
           "target_cents":5,"stop_cents":5,"setup":None,"timing":None,
           "analogs":0,"validation":None,"historical":None,"wait_reasons":[],
           "confidence":None,"method":"sampled_first_touch_5c_5h_v1"}
    quote = usable_quote(fix,now)
    if not quote:
        out["wait_reasons"].append("A genuine FIX quote updated within 30 seconds is required.")
        return out
    bid,ask,at = quote
    pricing = {"bid":bid,"ask":ask}
    mid = (bid+ask)/2
    out.update(spot=round(mid,5),quote_at=at.isoformat(),spread_cents=round((ask-bid)*100,3))
    clean = {}
    for ts,p in points:
        ts = aware(ts)
        if ts and ts <= now and p and isfinite(p) and p > 0:
            clean[ts] = float(p)
    points = sorted(clean.items())
    f = features(points,[p[0] for p in points],now,mid)
    out["news"] = assess_news(payload.get("news_watch"),points,fix,now=now,require_recent_article=False)
    out["wait_reasons"].extend(out["news"]["wait_reasons"])
    if not (payload.get("market_state") or {}).get("is_open"):
        out["wait_reasons"].append("The FX market is closed or its status is unverified.")
    sources = payload.get("data_sources") or {}
    if sources.get("calendar") not in {"live","official"} or sources.get("calendar_status") != "LIVE" or payload.get("calendar_coverage_gaps"):
        out["wait_reasons"].append("Economic-calendar risk coverage is incomplete.")
    for event in upcoming_risk(payload,now):
        out["wait_reasons"].append(f"Reassess after the approaching report: {event}.")
    if f is None:
        out["wait_reasons"].append("Genuine 5-, 15- and 60-minute price history is required.")
        return out
    out["momentum_bps"] = {"5m":round(f[0],2),"15m":round(f[1],2),"60m":round(f[2],2)}
    rows = windows(points)
    matches = neighbors([r for r in rows if r["end"] < now-LOOKBACK],f)
    out.update(history_windows=len(rows),analogs=len(matches))
    if len(matches) < MIN_ANALOGS:
        out["wait_reasons"].append(f"Only {len(matches)} comparable independent five-hour paths; need {MIN_ANALOGS}. Gaps over two minutes cannot establish target/stop order.")
        return out
    direction,stats = choose(matches,pricing)
    out["historical"] = stats
    if direction is None:
        out["wait_reasons"].append("Neither direction has positive simulated net results for this setup.")
        return out
    out["direction"] = direction
    if out["news"].get("direction") and out["news"]["direction"] != direction:
        out["wait_reasons"].append("Fresh news and its price reaction oppose this direction.")
    sign = 1 if direction == "BUY_USD" else -1
    entry = ask if sign == 1 else bid
    target,stop = entry+sign*DISTANCE,entry-sign*DISTANCE
    out["setup"] = {"entry_rate":round(entry,6),"target_rate":round(target,6),"stop_rate":round(stop,6),
                    "target_net_usd":net_pnl(direction,entry,target),"stop_net_usd":net_pnl(direction,entry,stop),
                    "review_at":(now+MAX_HOLD).isoformat(),"basis":"Executable entry-to-exit rates; $100K reference, $40 fees"}
    if stats["target_hit_minutes_median"] is not None:
        out["timing"] = {"median_minutes":stats["target_hit_minutes_median"],
                         "label":"Median time among comparable historical target hits; not a deadline or guarantee"}
    validation = validate(rows,direction,pricing)
    out["validation"] = validation
    if validation["samples"] < MIN_VALIDATION or (validation["average_net_lower_99_usd"] or 0) <= 0 or (validation["target_first_lower_95_pct"] or 0) <= 50:
        out["wait_reasons"].append("Earlier walk-forward target/stop tests have not established positive net evidence.")
    if stats["samples"] < MIN_ANALOGS or (stats["average_net_lower_99_usd"] or 0) <= 0 or (stats["target_first_lower_95_pct"] or 0) <= 50:
        out["wait_reasons"].append("Comparable results are too uncertain after costs.")
    if ask-bid >= DISTANCE:
        out["wait_reasons"].append("The current spread consumes the entire five-cent stop distance.")
    if not out["wait_reasons"]:
        out["action"] = "BUY USD / SELL MXN" if sign == 1 else "SELL USD / BUY MXN"
    out["limitations"] = "Snapshots can miss touches between checks. Simulations hold today's spread constant, include stop overshoot and $40 fees, and exclude other slippage. No live probability is calibrated."
    return out


def current(db, payload):
    from app.services.fix.provider import get_fix_quote
    from app.services.fx_observations import capture, points
    now = datetime.now(timezone.utc)
    fix = get_fix_quote()
    capture(db,fix,now)
    db.commit()
    return build(points(db,now),fix,payload,now=now)
