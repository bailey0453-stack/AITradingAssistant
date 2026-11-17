"""Independent 20-minute FIX forecast using real, timed intraday outcomes.

No scaling of hourly forecasts, synthetic history, or borrowed confidence.
Nearest-neighbor estimates are experimental. A purged walk-forward check must
support the current direction before the card can recommend acting.
"""

from __future__ import annotations

from bisect import bisect_left
from datetime import datetime, timedelta, timezone
from math import isfinite, sqrt
from statistics import mean
from threading import Lock
from time import monotonic
from weakref import WeakKeyDictionary

from sqlalchemy import select

from app.models import MarketSnapshot
from app.services.hedge_pnl import hedge_pnl_detail
from app.services.trade_decision_card import _blocking_event
from app.services.short_term_news import assess as assess_news
from app.services.five_cent_plan import apply as apply_five_cent_plan, settings as five_cent_settings

MIN_ANALOGS = 30
MIN_VALIDATION = 30
MAX_ROWS = 20_000
HORIZON = timedelta(minutes=20)
TOLERANCE = timedelta(seconds=120)
_history_cache = WeakKeyDictionary()
_history_lock = Lock()


def _number(value):
    try:
        value = float(value)
        return value if isfinite(value) and value > 0 else None
    except (TypeError, ValueError):
        return None


def _time(value):
    try:
        value = value if isinstance(value, datetime) else datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)
    except (ValueError, TypeError):
        return None


def _nearest(points, times, at):
    i = bisect_left(times, at)
    candidates = [points[j] for j in (i - 1, i) if 0 <= j < len(points)]
    point = min(candidates, key=lambda p: abs(p[0] - at), default=None)
    return point if point and abs(point[0] - at) <= TOLERANCE else None


def _features(points, times, at, spot):
    baselines = [_nearest(points, times, at - timedelta(minutes=m)) for m in (5, 15)]
    if any(p is None or p[0] >= at for p in baselines):
        return None
    # Relative changes in basis points; historical and current features use
    # exactly the same transform, with no data-dependent future normalization.
    return tuple((spot / p[1] - 1) * 10_000 for p in baselines)


def _examples(points):
    times = [p[0] for p in points]
    examples = []
    last_end = None
    for at, spot in points:
        # Full [lookback, outcome] windows do not overlap; duplicated requests
        # cannot inflate the effective sample count.
        if last_end is not None and at - timedelta(minutes=15) <= last_end:
            continue
        features = _features(points, times, at, spot)
        end = _nearest(points, times, at + HORIZON)
        if features is None or end is None or end[0] <= at:
            continue
        start_i = bisect_left(times, at - timedelta(minutes=15))
        end_i = bisect_left(times, end[0])
        if any(times[i + 1] - times[i] > timedelta(minutes=8) for i in range(start_i, end_i)):
            continue
        examples.append({"at": at, "end": end[0], "features": features, "return": end[1] / spot - 1})
        last_end = end[0]
    return examples


def _neighbors(examples, features):
    # A fixed radius rejects unfamiliar momentum instead of extrapolating from
    # arbitrarily distant examples. Scales are 2 bp (5m) and 4 bp (15m).
    ranked = []
    for row in examples:
        distance = sum(((a - b) / scale) ** 2 for a, b, scale in zip(row["features"], features, (2, 4)))
        if distance <= 4:
            ranked.append((distance, row))
    return [row for _, row in sorted(ranked, key=lambda pair: pair[0])[:60]]


def _wilson_lower(wins, count):
    if not count:
        return 0.0
    p, z = wins / count, 1.96
    return (p + z*z/(2*count) - z*sqrt(p*(1-p)/count + z*z/(4*count*count))) / (1 + z*z/count)


def _validation(examples, direction, fix):
    results = []
    # Each historical forecast sees only examples whose outcomes preceded that
    # forecast's entire lookback window. No random train/test split or leakage.
    for i in range(max(MIN_ANALOGS, len(examples) - 120), len(examples)):
        row = examples[i]
        training = [r for r in examples[:i] if r["end"] < row["at"] - timedelta(minutes=15)]
        neighbors = _neighbors(training, row["features"])
        if len(neighbors) < MIN_ANALOGS:
            continue
        predicted = mean(r["return"] for r in neighbors)
        if predicted == 0 or ("BUY_USD" if predicted > 0 else "SELL_USD") != direction:
            continue
        mid = (fix["bid"] + fix["ask"]) / 2
        pnl = hedge_pnl_detail(direction, mid * (1 + row["return"]), fix)
        results.append((predicted * row["return"] > 0, pnl["net_pnl_usd"]))
    n = len(results)
    wins = sum(r[0] for r in results)
    return {
        "samples": n,
        "directional_accuracy_pct": round(wins / n * 100, 1) if n else None,
        "accuracy_lower_95_pct": round(_wilson_lower(wins, n) * 100, 1) if n else None,
        "average_net_pnl_usd": round(mean(r[1] for r in results), 2) if n else None,
        "status": "walk_forward_simulation",
        "cost_basis": "Current FIX spread held constant, $100K notional, $40 round-trip fees; excludes slippage",
    }


def build(points, fix, payload, *, now=None):
    """Pure forecast builder; points must be genuine FIX (timestamp, midpoint)."""
    now = _time(now or datetime.now(timezone.utc))
    out = {
        "action": "WAIT", "horizon_minutes": 20, "generated_at": now.isoformat(),
        "expires_at": (now + timedelta(seconds=60)).isoformat(),
        "forecast_for": (now + HORIZON).isoformat(),
        "status": "unavailable", "direction": None, "expected_rate": None,
        "expected_move_mxn": None, "expected_move_cents": None,
        "outcome_range": None, "net_pnl_usd": None, "analogs": 0,
        "validation": None, "wait_reasons": [], "method": "intraday_neighbors_20m_news_gated_v2",
        "news": {"status": "UNAVAILABLE", "headlines": []},
        "five_cent_plan": five_cent_settings(),
    }
    def wait(reason):
        out["wait_reasons"].append(reason)
        return out
    fix = fix or {}
    bid, ask, quoted_at = _number(fix.get("bid")), _number(fix.get("ask")), _time(fix.get("updated_at"))
    if bid is None or ask is None or ask < bid or quoted_at is None or not -2 <= (now - quoted_at).total_seconds() <= 30:
        return wait("A live FIX bid/ask updated within 30 seconds is required.")
    spot = (bid + ask) / 2
    out.update(spot=round(spot, 5), quote_at=quoted_at.isoformat())
    if not (payload.get("market_state") or {}).get("is_open"):
        return wait("The FX market is closed or its open status is unverified.")
    points = sorted({at: float(value) for ts, value in points if (at := _time(ts)) and at <= now and _number(value) is not None}.items())
    out["news"] = assess_news(payload.get("news_watch"), points, fix, now=now)
    if out["news"]["wait_reasons"]:
        out["wait_reasons"].extend(out["news"]["wait_reasons"])
        return out
    # Re-anchor the live features to the executable FIX midpoint.
    features = _features(points, [p[0] for p in points], now, spot)
    if features is None:
        return wait("Genuine 5-minute and 15-minute FIX history is missing; hourly forecasts cannot supply a 20-minute estimate.")
    examples = _examples(points)
    neighbors = _neighbors([r for r in examples if r["end"] < now - timedelta(minutes=15)], features)
    out["analogs"] = len(neighbors)
    out["history_windows"] = len(examples)
    if len(neighbors) < MIN_ANALOGS:
        return wait(f"Only {len(neighbors)} comparable non-overlapping 20-minute outcomes; need {MIN_ANALOGS}.")
    returns = sorted(r["return"] for r in neighbors)
    expected = spot * (1 + mean(returns))
    move = expected - spot
    direction = "BUY_USD" if move > 0 else "SELL_USD" if move < 0 else None
    out.update(status="experimental", direction=direction, expected_rate=round(expected, 5),
               expected_move_mxn=round(move, 5), expected_move_cents=round(move * 100, 3),
               outcome_range={"low": round(spot*(1+returns[int((len(returns)-1)*.1)]), 5),
                              "high": round(spot*(1+returns[int((len(returns)-1)*.9)]), 5),
                              "label": "Central 80% of analogous outcomes; uncalibrated forecast range"})
    if direction is None:
        return wait("Comparable outcomes imply no directional move.")
    if out["news"].get("direction") and out["news"]["direction"] != direction:
        out.update(direction=None, expected_rate=None, expected_move_mxn=None,
                   expected_move_cents=None, outcome_range=None)
        return wait("Fresh news and its observed price reaction oppose the price-analog forecast; target suspended.")
    pricing = {"bid": bid, "ask": ask}
    pnl = hedge_pnl_detail(direction, expected, pricing)
    out["net_pnl_usd"] = pnl["net_pnl_usd"]
    out["cost_basis"] = "Estimated net for $100K, current FIX spread held at exit, $40 fees; excludes slippage"
    validation = _validation(examples, direction, pricing)
    out["validation"] = validation
    if validation["samples"] < MIN_VALIDATION or (validation["accuracy_lower_95_pct"] or 0) <= 50 or (validation["average_net_pnl_usd"] or 0) <= 0:
        wait("20-minute walk-forward evidence has not established an edge after estimated costs.")
    if pnl["net_pnl_usd"] <= 0:
        wait("The estimated move does not cover the FIX spread and round-trip fees.")
    # Reuse the established event/invalidation restrictions; never derive a
    # 20-minute confidence percentage from the existing 4-hour confidence.
    event = _blocking_event(payload)
    if event:
        wait(f"High-impact event risk: {event['name']}.")
    sources = payload.get("data_sources") or {}
    if sources.get("calendar") not in {"live", "official"} or sources.get("calendar_status") != "LIVE" or payload.get("calendar_coverage_gaps"):
        wait("Live economic-calendar coverage is incomplete or unverified.")
    if payload.get("invalidated") or (payload.get("trade_decision_card") or {}).get("action") == "EXIT / INVALIDATED":
        wait("The current trade thesis has been invalidated.")
    if not out["wait_reasons"]:
        out["action"] = "BUY USD / SELL MXN NOW" if direction == "BUY_USD" else "SELL USD / BUY MXN NOW"
    return apply_five_cent_plan(out, direction, pricing)


def from_database(db, payload):
    from app.services.fix.provider import get_fix_quote

    now = datetime.now(timezone.utc)
    # Bounded, column-only read of real intraday FIX snapshots. Daily research
    # data, mock data and hourly fallback quotes never enter this model.
    query = select(MarketSnapshot.created_at, MarketSnapshot.usdmxn)
    query = query.where(MarketSnapshot.pair == "USDMXN", MarketSnapshot.provider == "centroid_fix",
                        MarketSnapshot.source == "live", MarketSnapshot.usdmxn > 0)
    engine = db.get_bind()
    with _history_lock:
        cached = _history_cache.get(engine)
        if cached is None or monotonic() - cached[0] > 300:
            history = db.execute(query
                .where(MarketSnapshot.created_at >= now - timedelta(days=60))
                .order_by(MarketSnapshot.created_at.desc()).limit(MAX_ROWS)).all()
            cached = (monotonic(), history)
            _history_cache[engine] = cached
    # Small fresh tail keeps 5m/15m features current without retransferring
    # the full history on every dashboard poll. Neither cache contains FIX quotes.
    recent = db.execute(query
        .where(MarketSnapshot.created_at >= now - timedelta(minutes=20))
        .order_by(MarketSnapshot.created_at.desc()).limit(1000)).all()
    rows = list(dict(cached[1] + recent).items())
    return build(rows, get_fix_quote(), payload, now=now)
