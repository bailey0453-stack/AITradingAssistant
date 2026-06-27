"""Evaluator for paper AI recommendations.

Scores stored :class:`Recommendation` rows once enough time has passed, using
the durable USD/MXN price history in ``market_snapshots``. Evaluation is
*batched and bounded* (``evaluate_due`` has a ``limit``) so it is never run as a
heavy calculation on a dashboard load — call it from ``POST
/recommendations/evaluate`` or a future scheduler.

Performance reads (``performance_summary``) aggregate already-scored outcomes,
which is cheap, so the dashboard stays fast.
"""

from __future__ import annotations

import logging
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from typing import Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import MarketSnapshot, Recommendation, RecommendationOutcome

logger = logging.getLogger(__name__)

# Ordered horizons and their fixed offsets (seconds). ``end_of_day`` is special:
# the next FX day close (21:00 UTC) at/after the recommendation.
HORIZON_SECONDS = {
    "1h": 3600,
    "4h": 4 * 3600,
    "end_of_day": None,
    "1d": 24 * 3600,
    "2d": 2 * 24 * 3600,
    "5d": 5 * 24 * 3600,
}
HORIZONS = list(HORIZON_SECONDS.keys())

_FX_DAY_CLOSE_HOUR = 21  # 21:00 UTC

# Paper hedge (SIMULATED model evaluation only — never a real trade).
PAPER_NOTIONAL_USD = 100_000.0
PAPER_ENTRY_COST_USD = 20.0
PAPER_EXIT_COST_USD = 20.0
PAPER_TOTAL_COST_USD = PAPER_ENTRY_COST_USD + PAPER_EXIT_COST_USD  # 40
_ACTIONABLE = {"BUY_USD", "SELL_USD"}
_DIR_SIGN = {"BUY_USD": 1.0, "SELL_USD": -1.0}


def _aware(dt: datetime) -> datetime:
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def horizon_due_time(created_at: datetime, horizon: str) -> datetime:
    """When a horizon becomes evaluable for a recommendation created at ``created_at``."""
    created = _aware(created_at)
    if horizon == "end_of_day":
        close = created.replace(
            hour=_FX_DAY_CLOSE_HOUR, minute=0, second=0, microsecond=0
        )
        if created >= close:
            close += timedelta(days=1)
        return close
    return created + timedelta(seconds=HORIZON_SECONDS[horizon])


def _price_window(
    db: Session, pair: str, start: datetime, end: datetime
) -> list[tuple[datetime, float]]:
    rows = db.execute(
        select(MarketSnapshot.created_at, MarketSnapshot.usdmxn)
        .where(MarketSnapshot.pair == pair)
        .where(MarketSnapshot.usdmxn.is_not(None))
        .where(MarketSnapshot.created_at >= start)
        .where(MarketSnapshot.created_at <= end)
        .order_by(MarketSnapshot.created_at.asc())
    ).all()
    return [(_aware(r[0]), float(r[1])) for r in rows if r[1] is not None]


def _evaluation_price(
    db: Session, pair: str, due: datetime, now: datetime
) -> Optional[tuple[datetime, float]]:
    """Closest snapshot to the horizon's due time (prefer first at/after due)."""
    after = db.execute(
        select(MarketSnapshot.created_at, MarketSnapshot.usdmxn)
        .where(MarketSnapshot.pair == pair)
        .where(MarketSnapshot.usdmxn.is_not(None))
        .where(MarketSnapshot.created_at >= due)
        .where(MarketSnapshot.created_at <= now)
        .order_by(MarketSnapshot.created_at.asc())
        .limit(1)
    ).first()
    if after and after[1] is not None:
        return _aware(after[0]), float(after[1])
    return None  # no post-horizon observation yet -> evaluate later


def _first_crossing_hours(
    series: list[tuple[datetime, float]], created: datetime,
    level: Optional[float], above: bool
) -> Optional[float]:
    """Hours from ``created`` to the first time price crosses ``level``.

    ``above=True`` means "price >= level" (target for BUY / stop for SELL);
    ``above=False`` means "price <= level".
    """
    if level is None:
        return None
    for ts, px in series:
        if (px >= level) if above else (px <= level):
            return round(max(0.0, (_aware(ts) - created).total_seconds()) / 3600, 3)
    return None


def _exit_price(
    spot_eval: float,
    target: Optional[float],
    stop: Optional[float],
    time_to_target: Optional[float],
    time_to_stop: Optional[float],
) -> float:
    """First-touch exit price for a paper hedge.

    Exit at the target if it was hit first, at the stop if that was hit first,
    otherwise at the nearest evaluation (horizon close) price. Ties (both levels
    first crossed within the same observed snapshot) resolve to the stop, which
    is the more conservative assumption for a risk-managed hedge.
    """
    hit_target = time_to_target is not None and target is not None
    hit_stop = time_to_stop is not None and stop is not None
    if hit_target and hit_stop:
        return stop if time_to_stop <= time_to_target else target
    if hit_target:
        return target
    if hit_stop:
        return stop
    return spot_eval


def _score(reco: Recommendation, spot_eval: float,
           series: list[tuple[datetime, float]],
           created: datetime, eval_time: datetime) -> dict:
    """Compute the outcome + paper-hedge metrics for one horizon."""
    spot0 = reco.spot_price
    ret = round((spot_eval - spot0) / spot0 * 100, 4) if spot0 else None
    prices = [p for _, p in series]
    hi = max(prices) if prices else spot_eval
    lo = min(prices) if prices else spot_eval

    direction = reco.direction
    if direction == "BUY_USD":
        correct = spot_eval > spot0 if spot0 else None
        target_hit = reco.target is not None and hi >= reco.target
        stretch_hit = reco.stretch_target is not None and hi >= reco.stretch_target
        stop_hit = reco.stop is not None and lo <= reco.stop
        mfe = (hi - spot0) / spot0 * 100 if spot0 else None
        mae = (lo - spot0) / spot0 * 100 if spot0 else None
        ttt = _first_crossing_hours(series, created, reco.target, above=True)
        tts = _first_crossing_hours(series, created, reco.stop, above=False)
    elif direction == "SELL_USD":
        correct = spot_eval < spot0 if spot0 else None
        target_hit = reco.target is not None and lo <= reco.target
        stretch_hit = reco.stretch_target is not None and lo <= reco.stretch_target
        stop_hit = reco.stop is not None and hi >= reco.stop
        mfe = (spot0 - lo) / spot0 * 100 if spot0 else None
        mae = (spot0 - hi) / spot0 * 100 if spot0 else None
        ttt = _first_crossing_hours(series, created, reco.target, above=False)
        tts = _first_crossing_hours(series, created, reco.stop, above=True)
    else:  # NO_TRADE / PASS: "correct" when price stayed essentially flat.
        correct = (abs(ret) <= 0.10) if ret is not None else None
        target_hit = stretch_hit = stop_hit = False
        mfe = (hi - spot0) / spot0 * 100 if spot0 else None
        mae = (lo - spot0) / spot0 * 100 if spot0 else None
        ttt = tts = None

    # Paper hedge: actionable directions only, with first-touch exit logic —
    # exit at the target if it was hit first, at the stop if that was hit first,
    # otherwise at the nearest evaluation (horizon close) price.
    actionable = direction in _ACTIONABLE
    hedge_ret = gross = net = None
    if actionable and spot0:
        exit_price = _exit_price(spot_eval, reco.target, reco.stop, ttt, tts)
        hedge_ret = round(_DIR_SIGN[direction] * (exit_price - spot0) / spot0 * 100, 4)
        gross = round(PAPER_NOTIONAL_USD * hedge_ret / 100, 2)
        net = round(gross - PAPER_TOTAL_COST_USD, 2)

    return {
        "spot_at_evaluation": round(spot_eval, 6),
        "return_pct": ret,
        "direction_correct": bool(correct) if correct is not None else None,
        "target_hit": bool(target_hit),
        "stretch_hit": bool(stretch_hit),
        "stop_hit": bool(stop_hit),
        "max_favorable_excursion": round(mfe, 4) if mfe is not None else None,
        "max_adverse_excursion": round(mae, 4) if mae is not None else None,
        "time_to_target_hours": ttt,
        "time_to_stop_hours": tts,
        "holding_time_hours": round((eval_time - created).total_seconds() / 3600, 3),
        "actionable": actionable,
        "hedge_return_pct": hedge_ret,
        "gross_pnl_usd": gross,
        "net_pnl_usd": net,
    }


def evaluate_due(
    db: Session, now: Optional[datetime] = None, limit: int = 200
) -> dict:
    """Score every due, unscored (recommendation, horizon) pair, bounded by ``limit``.

    Returns ``{"evaluated": n, "recommendations_touched": m, "completed": k}``.
    """
    now = _aware(now or datetime.now(timezone.utc))
    pending = db.execute(
        select(Recommendation)
        .where(Recommendation.evaluation_status != "complete")
        .order_by(Recommendation.created_at.asc())
    ).scalars().all()

    evaluated = 0
    touched = 0
    completed = 0

    for reco in pending:
        if evaluated >= limit:
            break
        existing = {
            o.horizon for o in db.execute(
                select(RecommendationOutcome).where(
                    RecommendationOutcome.recommendation_id == reco.id
                )
            ).scalars().all()
        }
        created = _aware(reco.created_at)
        touched_this = False
        for horizon in HORIZONS:
            if evaluated >= limit:
                break
            if horizon in existing:
                continue
            due = horizon_due_time(created, horizon)
            if now < due:
                continue  # not enough time has passed yet
            ev = _evaluation_price(db, reco.pair, due, now)
            if ev is None:
                continue  # no price observed at/after the horizon yet
            eval_time, spot_eval = ev
            series = _price_window(db, reco.pair, created, eval_time)
            metrics = _score(reco, spot_eval, series, created, eval_time)
            db.add(RecommendationOutcome(
                recommendation_id=reco.id, horizon=horizon,
                evaluated_at=now, **metrics,
            ))
            existing.add(horizon)
            evaluated += 1
            touched_this = True

        if touched_this:
            touched += 1
            reco.last_evaluated_at = now
            if existing.issuperset(HORIZONS):
                reco.evaluation_status = "complete"
                completed += 1
            else:
                reco.evaluation_status = "partial"

    if evaluated or touched:
        db.commit()
    return {"evaluated": evaluated, "recommendations_touched": touched,
            "completed": completed}


# --- Performance aggregation (cheap; reads scored outcomes) -----------------
_CONFIDENCE_BUCKETS = [
    ("0-50", 0.0, 50.0),
    ("50-70", 50.0, 70.0),
    ("70-85", 70.0, 85.0),
    ("85-100", 85.0, 100.01),
]


def _bucket(confidence: Optional[float]) -> str:
    if confidence is None:
        return "unknown"
    for name, lo, hi in _CONFIDENCE_BUCKETS:
        if lo <= confidence < hi:
            return name
    return "unknown"


def _agg(rows: list[dict]) -> dict:
    """Aggregate a list of outcome dicts into summary stats."""
    n = len(rows)
    if not n:
        return {"samples": 0, "win_rate": None, "target_hit_rate": None,
                "stop_hit_rate": None, "avg_return_pct": None}

    def rate(key):
        vals = [r[key] for r in rows if r[key] is not None]
        return round(100 * sum(1 for v in vals if v) / len(vals), 1) if vals else None

    returns = [r["return_pct"] for r in rows if r["return_pct"] is not None]
    return {
        "samples": n,
        "win_rate": rate("direction_correct"),
        "target_hit_rate": rate("target_hit"),
        "stop_hit_rate": rate("stop_hit"),
        "avg_return_pct": round(sum(returns) / len(returns), 4) if returns else None,
    }


def performance_summary(db: Session, max_outcomes: int = 10000) -> dict:
    """Summarize scored outcomes by confidence bucket, grade, and horizon.

    Bounded by ``max_outcomes`` (most recent) so it stays fast as data grows.
    """
    total_recs = db.execute(
        select(Recommendation.id)
    ).scalars().all()
    total = len(total_recs)

    pairs = db.execute(
        select(RecommendationOutcome, Recommendation)
        .join(Recommendation, RecommendationOutcome.recommendation_id == Recommendation.id)
        .order_by(RecommendationOutcome.evaluated_at.desc())
        .limit(max_outcomes)
    ).all()

    flat: list[dict] = []
    by_conf: dict[str, list[dict]] = defaultdict(list)
    by_grade: dict[str, list[dict]] = defaultdict(list)
    by_horizon: dict[str, list[dict]] = defaultdict(list)

    for outcome, reco in pairs:
        row = {
            "return_pct": outcome.return_pct,
            "direction_correct": outcome.direction_correct,
            "target_hit": outcome.target_hit,
            "stop_hit": outcome.stop_hit,
        }
        flat.append(row)
        by_conf[_bucket(reco.confidence)].append(row)
        by_grade[reco.opportunity_grade or "n/a"].append(row)
        by_horizon[outcome.horizon].append(row)

    overall = _agg(flat)
    return {
        "total_recommendations": total,
        "evaluated_outcomes": len(flat),
        "overall": overall,
        "win_rate": overall["win_rate"],
        "target_hit_rate": overall["target_hit_rate"],
        "stop_hit_rate": overall["stop_hit_rate"],
        "avg_return_pct": overall["avg_return_pct"],
        "by_confidence": {k: _agg(v) for k, v in sorted(by_conf.items())},
        "by_grade": {k: _agg(v) for k, v in sorted(by_grade.items())},
        "by_horizon": {h: _agg(by_horizon.get(h, [])) for h in HORIZONS},
    }
