"""AI Research Lab — self-evaluation analytics over paper recommendations.

All metrics are computed from already-scored ``recommendation_outcomes`` (cheap
reads). Headline accuracy / calibration / paper-hedge use a single
``PRIMARY_HORIZON`` so samples are not double-counted; per-horizon breakdowns are
provided separately.

Paper hedge figures are **SIMULATED model evaluation only** — no real trades.
Self-assessment produces *observations only*; it never changes weights.
"""

from __future__ import annotations

from collections import defaultdict
from typing import Callable, Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Recommendation, RecommendationOutcome
from app.services import cache_manager
from app.services.recommendation_evaluator import (
    HORIZONS,
    PAPER_NOTIONAL_USD,
    PAPER_TOTAL_COST_USD,
)

PRIMARY_HORIZON = "1d"

_CONFIDENCE_BUCKETS = [("0-50", 0.0, 50.0), ("50-70", 50.0, 70.0),
                      ("70-85", 70.0, 85.0), ("85-100", 85.0, 100.01)]
_SIM_BUCKETS = [("<0.6", -1.0, 0.6), ("0.6-0.8", 0.6, 0.8), (">=0.8", 0.8, 1.01)]
_VOL_BUCKETS = [("low", -1.0, 14.0), ("normal", 14.0, 20.0), ("high", 20.0, 999.0)]


# --- loaders ----------------------------------------------------------------
def _pairs(db: Session, horizon: Optional[str] = None, limit: int = 50000):
    q = (
        select(RecommendationOutcome, Recommendation)
        .join(Recommendation, RecommendationOutcome.recommendation_id == Recommendation.id)
        .order_by(RecommendationOutcome.evaluated_at.desc())
        .limit(limit)
    )
    rows = db.execute(q).all()
    if horizon:
        rows = [(o, r) for (o, r) in rows if o.horizon == horizon]
    return rows


# --- small stats helpers ----------------------------------------------------
def _rate(vals) -> Optional[float]:
    vals = [v for v in vals if v is not None]
    return round(100 * sum(1 for v in vals if v) / len(vals), 1) if vals else None


def _mean(vals) -> Optional[float]:
    vals = [v for v in vals if v is not None]
    return round(sum(vals) / len(vals), 4) if vals else None


def _accuracy_block(pairs) -> dict:
    outs = [o for o, _ in pairs]
    return {
        "samples": len(outs),
        "accuracy": _rate([o.direction_correct for o in outs]),
        "target_hit_rate": _rate([o.target_hit for o in outs]),
        "stop_hit_rate": _rate([o.stop_hit for o in outs]),
        "avg_return_pct": _mean([o.return_pct for o in outs]),
        "avg_net_pnl_usd": _mean([o.net_pnl_usd for o in outs if o.actionable]),
    }


def _group(pairs, keyfn: Callable) -> dict:
    buckets = defaultdict(list)
    for o, r in pairs:
        buckets[keyfn(o, r)].append((o, r))
    return {str(k): _accuracy_block(v) for k, v in sorted(buckets.items(), key=lambda x: str(x[0]))}


def _bucket(value, buckets, default="unknown") -> str:
    if value is None:
        return default
    for name, lo, hi in buckets:
        if lo <= value < hi:
            return name
    return default


def _sim_value(hs) -> Optional[float]:
    if not isinstance(hs, dict):
        return None
    for key in ("best_similarity", "similarity", "score", "similarity_score"):
        v = hs.get(key)
        if isinstance(v, (int, float)):
            return float(v)
    return None


# --- accuracy / research summary -------------------------------------------
def research_summary(db: Session) -> dict:
    pairs = _pairs(db, horizon=PRIMARY_HORIZON)
    all_pairs = _pairs(db)  # every horizon, for by-horizon view

    overall = _accuracy_block(pairs)
    by_conf = _group(pairs, lambda o, r: _bucket(r.confidence, _CONFIDENCE_BUCKETS))
    by_grade = _group(pairs, lambda o, r: r.opportunity_grade or "n/a")
    by_regime = _group(pairs, lambda o, r: r.regime or "unknown")
    by_news = _group(pairs, lambda o, r: r.news_category or "none")
    by_sim = _group(pairs, lambda o, r: _bucket(_sim_value(r.historical_similarity), _SIM_BUCKETS))
    by_vol = _group(pairs, lambda o, r: _bucket(r.volatility, _VOL_BUCKETS))
    by_horizon = _group(all_pairs, lambda o, r: o.horizon)
    by_model = _group(pairs, lambda o, r: r.model_version or "unknown")

    drivers = driver_stats(db)
    return {
        "primary_horizon": PRIMARY_HORIZON,
        "overall_accuracy": overall["accuracy"],
        "overall": overall,
        "accuracy_by_confidence": by_conf,
        "accuracy_by_grade": by_grade,
        "accuracy_by_regime": by_regime,
        "accuracy_by_news_category": by_news,
        "accuracy_by_historical_similarity": by_sim,
        "accuracy_by_volatility": by_vol,
        "accuracy_by_time_horizon": by_horizon,
        "accuracy_by_model_version": by_model,
        "confidence_calibration": calibration(db)["buckets"],
        "signal_stability": signal_stability(db),
        "top_drivers": drivers["top"],
        "weakest_drivers": drivers["weakest"],
        "historical_similarity_accuracy": by_sim,
        "provider_reliability": cache_manager.health_snapshot(),
        "self_assessment": self_assessment(db),
    }


def calibration(db: Session) -> dict:
    """Predicted confidence vs actual accuracy, per confidence bucket."""
    pairs = _pairs(db, horizon=PRIMARY_HORIZON)
    buckets = {}
    for name, lo, hi in _CONFIDENCE_BUCKETS:
        sub = [(o, r) for o, r in pairs if r.confidence is not None and lo <= r.confidence < hi]
        outs = [o for o, _ in sub]
        actual = _rate([o.direction_correct for o in outs])
        predicted = _mean([r.confidence for _, r in sub])
        buckets[name] = {
            "samples": len(outs),
            "predicted_confidence": predicted,
            "actual_accuracy": actual,
            "gap": (round(predicted - actual, 1) if (predicted is not None and actual is not None) else None),
        }
    return {"horizon": PRIMARY_HORIZON, "buckets": buckets}


def _driver_labels(reco: Recommendation) -> list[str]:
    out: list[str] = []
    for coll in (reco.key_drivers, reco.bullish_factors, reco.bearish_factors):
        for item in coll or []:
            if isinstance(item, str):
                out.append(item)
            elif isinstance(item, dict):
                lbl = item.get("driver") or item.get("label") or item.get("name")
                if lbl:
                    out.append(str(lbl))
    return out


def driver_stats(db: Session, min_samples: int = 3) -> dict:
    pairs = _pairs(db, horizon=PRIMARY_HORIZON)
    by_driver = defaultdict(list)
    for o, r in pairs:
        for label in set(_driver_labels(r)):
            by_driver[label].append(o)
    stats = []
    for label, outs in by_driver.items():
        acc = _rate([o.direction_correct for o in outs])
        if acc is None or len(outs) < min_samples:
            continue
        stats.append({"driver": label, "samples": len(outs), "accuracy": acc,
                      "avg_return_pct": _mean([o.return_pct for o in outs])})
    stats.sort(key=lambda s: s["accuracy"], reverse=True)
    return {
        "all": stats,
        "top": stats[:5],
        "weakest": list(reversed(stats[-5:])) if len(stats) > 5 else list(reversed(stats)),
    }


def signal_stability(db: Session, limit: int = 500) -> dict:
    """Direction consistency + drift across recent recommendations."""
    rows = db.execute(
        select(Recommendation.direction)
        .order_by(Recommendation.created_at.desc())
        .limit(limit)
    ).scalars().all()
    if len(rows) < 2:
        return {"samples": len(rows), "stability": None, "drift_rate": None, "flips": 0}
    flips = sum(1 for a, b in zip(rows, rows[1:]) if a != b)
    n = len(rows) - 1
    return {
        "samples": len(rows),
        "flips": flips,
        "drift_rate": round(100 * flips / n, 1),
        "stability": round(100 * (1 - flips / n), 1),
    }


def model_performance(db: Session) -> dict:
    pairs = _pairs(db, horizon=PRIMARY_HORIZON)
    return {
        "primary_horizon": PRIMARY_HORIZON,
        "by_model_version": _group(pairs, lambda o, r: r.model_version or "unknown"),
    }


# --- paper hedge + monthly --------------------------------------------------
def paper_hedge_performance(db: Session) -> dict:
    pairs = _pairs(db, horizon=PRIMARY_HORIZON)
    actionable = [o for o, _ in pairs if o.actionable and o.net_pnl_usd is not None]
    nets = [o.net_pnl_usd for o in actionable]
    gross = [o.gross_pnl_usd for o in actionable if o.gross_pnl_usd is not None]
    return {
        "label": "SIMULATED PAPER PERFORMANCE",
        "notional_usd": PAPER_NOTIONAL_USD,
        "cost_per_trade_usd": PAPER_TOTAL_COST_USD,
        "primary_horizon": PRIMARY_HORIZON,
        "actionable_trades": len(actionable),
        "win_rate": _rate([o.direction_correct for o in actionable]),
        "gross_pnl_usd": round(sum(gross), 2) if gross else 0.0,
        "transaction_costs_usd": round(len(actionable) * PAPER_TOTAL_COST_USD, 2),
        "net_pnl_usd": round(sum(nets), 2) if nets else 0.0,
        "avg_pnl_usd": _mean(nets),
        "return_on_notional_pct": (
            round(sum(nets) / (len(actionable) * PAPER_NOTIONAL_USD) * 100, 4)
            if actionable else None
        ),
        "best_trade_usd": max(nets) if nets else None,
        "worst_trade_usd": min(nets) if nets else None,
    }


def _month_key(dt) -> str:
    return dt.strftime("%Y-%m") if dt else "unknown"


def monthly_performance(db: Session) -> dict:
    pairs = _pairs(db, horizon=PRIMARY_HORIZON)
    # Total recs per month (independent of evaluation).
    rec_rows = db.execute(select(Recommendation.created_at, Recommendation.direction)).all()
    total_by_month = defaultdict(int)
    for created, _ in rec_rows:
        total_by_month[_month_key(created)] += 1

    by_month = defaultdict(list)
    for o, r in pairs:
        by_month[_month_key(r.created_at)].append((o, r))

    months = {}
    for month in sorted(set(total_by_month) | set(by_month)):
        sub = by_month.get(month, [])
        actionable = [o for o, _ in sub if o.actionable and o.net_pnl_usd is not None]
        nets = [o.net_pnl_usd for o in actionable]
        gross = [o.gross_pnl_usd for o in actionable if o.gross_pnl_usd is not None]
        months[month] = {
            "total_recommendations": total_by_month.get(month, 0),
            "actionable_recommendations": len(actionable),
            "win_rate": _rate([o.direction_correct for o in actionable]),
            "gross_pnl_usd": round(sum(gross), 2) if gross else 0.0,
            "transaction_costs_usd": round(len(actionable) * PAPER_TOTAL_COST_USD, 2),
            "net_pnl_usd": round(sum(nets), 2) if nets else 0.0,
            "avg_pnl_usd": _mean(nets),
            "return_on_notional_pct": (
                round(sum(nets) / (len(actionable) * PAPER_NOTIONAL_USD) * 100, 4)
                if actionable else None
            ),
            "best_trade_usd": max(nets) if nets else None,
            "worst_trade_usd": min(nets) if nets else None,
            "by_confidence": _group(sub, lambda o, r: _bucket(r.confidence, _CONFIDENCE_BUCKETS)),
            "by_grade": _group(sub, lambda o, r: r.opportunity_grade or "n/a"),
            "by_regime": _group(sub, lambda o, r: r.regime or "unknown"),
            "by_model_version": _group(sub, lambda o, r: r.model_version or "unknown"),
            "by_time_horizon": _group(
                [(o, r) for o, r in _pairs(db) if _month_key(r.created_at) == month],
                lambda o, r: o.horizon,
            ),
        }
    return {"label": "SIMULATED PAPER PERFORMANCE", "months": months}


# --- self assessment (observations only) ------------------------------------
def self_assessment(db: Session) -> list[str]:
    obs: list[str] = []
    cal = calibration(db)["buckets"]
    high = cal.get("85-100", {})
    if high.get("samples", 0) >= 10 and high.get("gap") is not None and high["gap"] >= 10:
        obs.append("Confidence appears too optimistic: at 85-100% confidence the "
                   f"model was correct only {high['actual_accuracy']}% of the time.")

    summary_pairs = _pairs(db, horizon=PRIMARY_HORIZON)
    overall_acc = _rate([o.direction_correct for o, _ in summary_pairs])

    by_grade = _group(summary_pairs, lambda o, r: r.opportunity_grade or "n/a")
    a, b = by_grade.get("A"), by_grade.get("B")
    if a and b and a["samples"] >= 10 and b["samples"] >= 10:
        if (b["accuracy"] or 0) > (a["accuracy"] or 0):
            obs.append(f"Grade B ({b['accuracy']}%) is outperforming Grade A "
                       f"({a['accuracy']}%) — grade scaling may be miscalibrated.")

    sim = _group(summary_pairs, lambda o, r: _bucket(_sim_value(r.historical_similarity), _SIM_BUCKETS))
    hi_sim = sim.get(">=0.8")
    if hi_sim and hi_sim["samples"] >= 10 and overall_acc is not None and (hi_sim["accuracy"] or 0) > overall_acc:
        obs.append(f"Historical similarity >= 0.8 performs best "
                   f"({hi_sim['accuracy']}% vs {overall_acc}% overall).")

    drivers = driver_stats(db)["weakest"]
    if drivers and overall_acc is not None:
        worst = drivers[0]
        if (worst["accuracy"] or 0) + 10 < overall_acc:
            obs.append(f"'{worst['driver']}' signals underperform "
                       f"({worst['accuracy']}% vs {overall_acc}% overall).")

    mv = _group(summary_pairs, lambda o, r: r.model_version or "unknown")
    versioned = {k: v for k, v in mv.items() if k != "unknown" and v["samples"] >= 10}
    if len(versioned) >= 2:
        best = max(versioned.items(), key=lambda kv: kv[1]["accuracy"] or 0)
        worst = min(versioned.items(), key=lambda kv: kv[1]["accuracy"] or 0)
        if (best[1]["accuracy"] or 0) > (worst[1]["accuracy"] or 0):
            obs.append(f"Model {best[0]} ({best[1]['accuracy']}%) outperforms "
                       f"{worst[0]} ({worst[1]['accuracy']}%).")

    if not obs:
        obs.append("Not enough evaluated history yet to draw reliable conclusions.")
    return obs
