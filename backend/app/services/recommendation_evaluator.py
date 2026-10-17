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
from datetime import datetime, timedelta, timezone
from typing import Optional

from sqlalchemy import and_, case, func, literal, or_, select, union_all
from sqlalchemy.orm import Session, load_only

from app.models import MarketSnapshot, Recommendation, RecommendationOutcome

logger = logging.getLogger(__name__)

# Ordered horizons and their fixed offsets (seconds). ``end_of_day`` is special:
# the next FX day close (21:00 UTC) at/after the recommendation.
HORIZON_SECONDS = {
    "1h": 3600,
    "2h": 2 * 3600,
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
    return None


def _first_crossing_hours(
    series: list[tuple[datetime, float]], created: datetime,
    level: Optional[float], above: bool
) -> Optional[float]:
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
    else:
        correct = (abs(ret) <= 0.10) if ret is not None else None
        target_hit = stretch_hit = stop_hit = False
        mfe = (hi - spot0) / spot0 * 100 if spot0 else None
        mae = (lo - spot0) / spot0 * 100 if spot0 else None
        ttt = tts = None

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
    now = _aware(now or datetime.now(timezone.utc))
    empty = {"evaluated": 0, "recommendations_touched": 0, "completed": 0}
    if limit <= 0:
        return empty

    # A horizon is only evaluable once a stored price reaches its due
    # time. In particular, wall-clock time advancing over a closed weekend
    # must not trigger a price query for every pending horizon on every run.
    latest_prices = {
        pair: _aware(latest)
        for pair, latest in db.execute(
            select(MarketSnapshot.pair, func.max(MarketSnapshot.created_at))
            .where(MarketSnapshot.usdmxn.is_not(None))
            .where(MarketSnapshot.created_at <= now)
            .group_by(MarketSnapshot.pair)
        ).all()
    }
    eligible_pairs = []
    for pair, latest in latest_prices.items():
        last_day_close = latest.replace(
            hour=_FX_DAY_CLOSE_HOUR, minute=0, second=0, microsecond=0
        )
        if last_day_close > latest:
            last_day_close -= timedelta(days=1)
        eligible_horizons = []
        for horizon, seconds in HORIZON_SECONDS.items():
            # A recommendation created exactly at 21:00 belongs to the next
            # day's close, hence the strict comparison for end_of_day.
            due_by_last_price = (
                Recommendation.created_at < last_day_close
                if seconds is None else
                Recommendation.created_at <= latest - timedelta(seconds=seconds)
            )
            already_scored = (
                select(RecommendationOutcome.id)
                .where(RecommendationOutcome.recommendation_id == Recommendation.id)
                .where(RecommendationOutcome.horizon == horizon)
                .exists()
            )
            eligible_horizons.append(and_(due_by_last_price, ~already_scored))
        eligible_pairs.append(and_(
            Recommendation.pair == pair, or_(*eligible_horizons)
        ))
    if not eligible_pairs:
        return empty

    # Filter before limiting: old rows waiting for unavailable prices must not
    # consume the whole batch and starve later rows that can actually be scored.
    # Each selected recommendation has at least one missing, available horizon.
    pending = db.execute(
        select(Recommendation)
        .options(load_only(
            Recommendation.created_at, Recommendation.pair, Recommendation.spot_price,
            Recommendation.direction, Recommendation.target, Recommendation.stretch_target,
            Recommendation.stop, Recommendation.evaluation_status,
            Recommendation.last_evaluated_at, raiseload=True,
        ))
        .where(Recommendation.evaluation_status != "complete")
        .where(or_(*eligible_pairs))
        .order_by(Recommendation.created_at.asc(), Recommendation.id.asc())
        .limit(limit)
    ).scalars().all()
    if not pending:
        return empty

    existing_by_recommendation = {reco.id: set() for reco in pending}
    for recommendation_id, horizon in db.execute(
        select(RecommendationOutcome.recommendation_id, RecommendationOutcome.horizon)
        .where(RecommendationOutcome.recommendation_id.in_(existing_by_recommendation))
    ).all():
        existing_by_recommendation[recommendation_id].add(horizon)

    evaluated = 0
    touched = 0
    completed = 0

    for reco in pending:
        if evaluated >= limit:
            break
        existing = existing_by_recommendation[reco.id]
        created = _aware(reco.created_at)
        touched_this = False
        for horizon in HORIZONS:
            if evaluated >= limit:
                break
            if horizon in existing:
                continue
            due = horizon_due_time(created, horizon)
            if due > latest_prices[reco.pair]:
                continue
            ev = _evaluation_price(db, reco.pair, due, now)
            if ev is None:
                continue
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
    total = db.scalar(select(func.count()).select_from(Recommendation))

    # Preserve the global newest-outcomes window before grouping. Return only
    # aggregate rows, never the large recommendation JSON reports or history.
    window = (
        select(
            RecommendationOutcome.return_pct,
            RecommendationOutcome.direction_correct,
            RecommendationOutcome.target_hit,
            RecommendationOutcome.stop_hit,
            RecommendationOutcome.horizon,
            Recommendation.confidence,
            Recommendation.opportunity_grade,
        )
        .join(Recommendation, RecommendationOutcome.recommendation_id == Recommendation.id)
        .order_by(RecommendationOutcome.evaluated_at.desc())
        .limit(max_outcomes)
        .cte("performance_window")
    )
    columns = window.c
    confidence_bucket = case(
        *[((columns.confidence >= lo) & (columns.confidence < hi), name)
          for name, lo, hi in _CONFIDENCE_BUCKETS],
        else_="unknown",
    )
    grade = case(
        (columns.opportunity_grade.is_(None), "n/a"),
        (columns.opportunity_grade == "", "n/a"),
        else_=columns.opportunity_grade,
    )

    def aggregate(dimension, key, grouped=True):
        metrics = [func.count().label("samples")]
        for name in ("direction_correct", "target_hit", "stop_hit"):
            column = columns[name]
            metrics.extend([
                func.count(column).label(name + "_scored"),
                func.sum(case((column.is_(True), 1), else_=0)).label(name + "_hits"),
            ])
        metrics.append(func.avg(columns.return_pct).label("avg_return_pct"))
        query = select(literal(dimension).label("dimension"), key.label("key"), *metrics).select_from(window)
        return query.group_by(key) if grouped else query

    results = db.execute(union_all(
        aggregate("overall", literal(""), grouped=False),
        aggregate("confidence", confidence_bucket),
        aggregate("grade", grade),
        aggregate("horizon", columns.horizon),
    )).mappings().all()

    groups = {name: {} for name in ("overall", "confidence", "grade", "horizon")}
    for row in results:
        metrics = {"samples": row["samples"]}
        for name, output in (("direction_correct", "win_rate"),
                             ("target_hit", "target_hit_rate"),
                             ("stop_hit", "stop_hit_rate")):
            scored = row[name + "_scored"]
            metrics[output] = round(100 * row[name + "_hits"] / scored, 1) if scored else None
        avg = row["avg_return_pct"]
        metrics["avg_return_pct"] = round(float(avg), 4) if avg is not None else None
        groups[row["dimension"]][row["key"]] = metrics

    overall = groups["overall"][""]
    return {
        "total_recommendations": total,
        "evaluated_outcomes": overall["samples"],
        "overall": overall,
        "win_rate": overall["win_rate"],
        "target_hit_rate": overall["target_hit_rate"],
        "stop_hit_rate": overall["stop_hit_rate"],
        "avg_return_pct": overall["avg_return_pct"],
        "by_confidence": dict(sorted(groups["confidence"].items())),
        "by_grade": dict(sorted(groups["grade"].items())),
        "by_horizon": {h: groups["horizon"].get(h, _agg([])) for h in HORIZONS},
    }
