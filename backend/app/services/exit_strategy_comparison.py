"""Exit strategy comparison: fixed-horizon vs first-net-profit (simulated).

Decision support / paper performance only — never sends orders.
Does not overwrite ``recommendation_outcomes`` fixed-horizon rows.
"""

from __future__ import annotations

import statistics
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.models import MarketSnapshot, Recommendation, RecommendationOutcome
from app.models.exit_simulations import (
    EXIT_FIRST_NET_PROFIT,
    EXIT_FIXED_HORIZON,
    RecommendationExitSimulation,
)
from app.services.recommendation_evaluator import (
    PAPER_ENTRY_COST_USD,
    PAPER_EXIT_COST_USD,
    PAPER_NOTIONAL_USD,
    horizon_due_time,
)

_ACTIONABLE = {"BUY_USD", "SELL_USD"}
_POLICY_VERSION = "first-net-profit-v1"
_FIXED_VERSION = "fixed-horizon-1d-v1"
# Minimum post-entry observations required to claim a complete path.
_MIN_TICKS_FOR_COMPLETE = 3
_MAX_HORIZON_HOURS = 24.0
# Same half-spread assumption for both methods when bid/ask unavailable.
_HALF_SPREAD = 0.005


@dataclass(frozen=True)
class _CostConfig:
    notional_usd: float = PAPER_NOTIONAL_USD
    entry_cost_usd: float = PAPER_ENTRY_COST_USD
    exit_cost_usd: float = PAPER_EXIT_COST_USD
    half_spread: float = _HALF_SPREAD

    @property
    def total_cost_usd(self) -> float:
        return self.entry_cost_usd + self.exit_cost_usd


def cost_config() -> _CostConfig:
    """Shared simulated cost assumptions (identical for both exit methods)."""
    return _CostConfig()


def executable_entry_rate(direction: str, mid: float, *, half_spread: float) -> float:
    if direction == "BUY_USD":
        return float(mid + half_spread)
    if direction == "SELL_USD":
        return float(mid - half_spread)
    raise ValueError(f"Non-actionable direction: {direction}")


def executable_exit_rate(direction: str, mid: float, *, half_spread: float) -> float:
    if direction == "BUY_USD":
        return float(mid - half_spread)
    if direction == "SELL_USD":
        return float(mid + half_spread)
    raise ValueError(f"Non-actionable direction: {direction}")


def compute_round_trip_pnl(
    direction: str, entry_rate: float, exit_rate: float, cfg: _CostConfig
) -> dict:
    sign = 1.0 if direction == "BUY_USD" else -1.0
    hedge_ret = sign * (exit_rate - entry_rate) / entry_rate * 100.0
    gross = cfg.notional_usd * hedge_ret / 100.0
    costs = cfg.total_cost_usd
    return {
        "hedge_return_pct": round(hedge_ret, 4),
        "gross_pnl_usd": round(gross, 2),
        "costs_usd": round(costs, 2),
        "net_pnl_usd": round(gross - costs, 2),
    }


def load_price_ticks(
    db: Session, pair: str, start: datetime, end: datetime
) -> list[tuple]:
    """Midpoint ticks from market_snapshots (finest stored observations)."""
    rows = db.execute(
        select(MarketSnapshot.created_at, MarketSnapshot.usdmxn)
        .where(MarketSnapshot.pair == pair)
        .where(MarketSnapshot.usdmxn.is_not(None))
        .where(MarketSnapshot.created_at >= start)
        .where(MarketSnapshot.created_at <= end)
        .order_by(MarketSnapshot.created_at.asc())
    ).all()
    return [(_aware(r[0]), float(r[1]), None, None) for r in rows if r[1] is not None]


def _aware(dt: datetime) -> datetime:
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _median(xs: list[float]) -> Optional[float]:
    if not xs:
        return None
    return round(float(statistics.median(xs)), 2)


def _max_drawdown(nets: list[float]) -> float:
    equity = peak = mdd = 0.0
    for n in nets:
        equity += n
        peak = max(peak, equity)
        mdd = max(mdd, peak - equity)
    return round(mdd, 2)


def _conf_bucket(c: Optional[float]) -> str:
    if c is None:
        return "unknown"
    if c >= 80:
        return "80+"
    if c >= 70:
        return "70-79"
    if c >= 60:
        return "60-69"
    return "<60"


def _pct_within(holding_minutes: list[float], limit_min: float) -> Optional[float]:
    if not holding_minutes:
        return None
    n = sum(1 for m in holding_minutes if m is not None and m <= limit_min)
    return round(100.0 * n / len(holding_minutes), 1)


def simulate_first_net_profit(
    *,
    direction: str,
    entry_at: datetime,
    spot_mid: float,
    ticks: list[tuple],
    cfg,
    max_horizon_hours: float = _MAX_HORIZON_HOURS,
    terminal_mid: Optional[float] = None,
    terminal_at: Optional[datetime] = None,
) -> dict:
    """Exit at the first observation where *net* P/L > 0 after costs.

    Look-ahead safe: ticks must already be sorted and after entry; stops at first hit.
    If none, falls back to terminal evaluation (fixed-horizon mid).
    """
    entry_at = _aware(entry_at)
    entry_rate = executable_entry_rate(direction, spot_mid, half_spread=cfg.half_spread)
    deadline = entry_at + timedelta(hours=max_horizon_hours)

    for ts, mid, *_rest in ticks:
        ts = _aware(ts)
        if ts <= entry_at:
            continue
        if ts > deadline:
            break
        exit_rate = executable_exit_rate(direction, float(mid), half_spread=cfg.half_spread)
        pnl = compute_round_trip_pnl(direction, entry_rate, exit_rate, cfg)
        net = pnl["net_pnl_usd"]
        if net is not None and net > 0:
            holding = (ts - entry_at).total_seconds() / 60.0
            return {
                "exit_policy": EXIT_FIRST_NET_PROFIT,
                "exit_policy_version": _POLICY_VERSION,
                "simulated_exit_at": ts,
                "simulated_exit_rate": round(exit_rate, 6),
                "simulated_exit_reason": "FIRST_NET_PROFIT",
                "simulated_gross_pnl": pnl["gross_pnl_usd"],
                "simulated_costs": pnl["costs_usd"],
                "simulated_net_pnl": pnl["net_pnl_usd"],
                "holding_minutes": round(holding, 2),
                "data_completeness": "complete",
            }

    # Terminal fallback
    if terminal_mid is None:
        return {
            "exit_policy": EXIT_FIRST_NET_PROFIT,
            "exit_policy_version": _POLICY_VERSION,
            "simulated_exit_at": None,
            "simulated_exit_rate": None,
            "simulated_exit_reason": "NO_TERMINAL_PRICE",
            "simulated_gross_pnl": None,
            "simulated_costs": cfg.total_cost_usd,
            "simulated_net_pnl": None,
            "holding_minutes": None,
            "data_completeness": "incomplete",
        }
    t_at = _aware(terminal_at or deadline)
    exit_rate = executable_exit_rate(direction, float(terminal_mid), half_spread=cfg.half_spread)
    pnl = compute_round_trip_pnl(direction, entry_rate, exit_rate, cfg)
    holding = (t_at - entry_at).total_seconds() / 60.0
    return {
        "exit_policy": EXIT_FIRST_NET_PROFIT,
        "exit_policy_version": _POLICY_VERSION,
        "simulated_exit_at": t_at,
        "simulated_exit_rate": round(exit_rate, 6),
        "simulated_exit_reason": "TERMINAL_FALLBACK",
        "simulated_gross_pnl": pnl["gross_pnl_usd"],
        "simulated_costs": pnl["costs_usd"],
        "simulated_net_pnl": pnl["net_pnl_usd"],
        "holding_minutes": round(max(0.0, holding), 2),
        "data_completeness": "complete",
    }


def simulate_fixed_horizon(
    *,
    direction: str,
    entry_at: datetime,
    spot_mid: float,
    terminal_mid: float,
    terminal_at: datetime,
    cfg,
) -> dict:
    entry_rate = executable_entry_rate(direction, spot_mid, half_spread=cfg.half_spread)
    exit_rate = executable_exit_rate(direction, float(terminal_mid), half_spread=cfg.half_spread)
    pnl = compute_round_trip_pnl(direction, entry_rate, exit_rate, cfg)
    holding = (_aware(terminal_at) - _aware(entry_at)).total_seconds() / 60.0
    return {
        "exit_policy": EXIT_FIXED_HORIZON,
        "exit_policy_version": _FIXED_VERSION,
        "simulated_exit_at": _aware(terminal_at),
        "simulated_exit_rate": round(exit_rate, 6),
        "simulated_exit_reason": "FIXED_HORIZON_1D",
        "simulated_gross_pnl": pnl["gross_pnl_usd"],
        "simulated_costs": pnl["costs_usd"],
        "simulated_net_pnl": pnl["net_pnl_usd"],
        "holding_minutes": round(max(0.0, holding), 2),
        "data_completeness": "complete",
    }


def _aggregate(trades: list[dict], *, label: str) -> dict:
    nets = [t["simulated_net_pnl"] for t in trades if t.get("simulated_net_pnl") is not None]
    grosses = [t["simulated_gross_pnl"] for t in trades if t.get("simulated_gross_pnl") is not None]
    holds = [t["holding_minutes"] for t in trades if t.get("holding_minutes") is not None]
    costs = [t["simulated_costs"] for t in trades if t.get("simulated_costs") is not None]
    wins = [n for n in nets if n > 0]
    losses = [n for n in nets if n <= 0]
    profitable_holds = [
        t["holding_minutes"]
        for t in trades
        if t.get("simulated_exit_reason") == "FIRST_NET_PROFIT"
        and t.get("holding_minutes") is not None
    ]
    # For fixed horizon, "exiting profitably within X" = net>0 and holding <= X
    # For first-profit, same using actual exit time; for terminal fallback use holding.
    within_source = [
        t["holding_minutes"]
        for t in trades
        if t.get("simulated_net_pnl") is not None
        and t["simulated_net_pnl"] > 0
        and t.get("holding_minutes") is not None
    ]
    n = len(nets)
    return {
        "method": label,
        "eligible_recommendations": None,  # filled by caller
        "evaluated_trades": n,
        "wins": len(wins),
        "losses": len(losses),
        "win_rate": round(100.0 * len(wins) / n, 1) if n else None,
        "gross_profit": round(sum(wins), 2) if wins else 0.0,
        "gross_loss": round(sum(losses), 2) if losses else 0.0,
        "transaction_costs": round(sum(costs), 2) if costs else 0.0,
        "net_pnl": round(sum(nets), 2) if nets else 0.0,
        "average_pnl": round(sum(nets) / n, 2) if n else None,
        "median_pnl": _median(nets),
        "average_holding_minutes": round(sum(holds) / len(holds), 1) if holds else None,
        "maximum_drawdown": _max_drawdown(nets),
        "best_trade": round(max(nets), 2) if nets else None,
        "worst_trade": round(min(nets), 2) if nets else None,
        "pct_exiting_profitably_within": {
            "1_hour": _pct_within(within_source, 60),
            "4_hours": _pct_within(within_source, 240),
            "end_of_day": _pct_within(within_source, 12 * 60),  # ~FX session proxy
            "1_day": _pct_within(within_source, 24 * 60),
        },
        "first_profit_exit_count": len(profitable_holds),
    }


def _breakdown(trades: list[dict], key: str) -> dict:
    buckets: dict[str, list[dict]] = defaultdict(list)
    for t in trades:
        buckets[str(t.get(key) or "unknown")].append(t)
    out = {}
    for k, rows in sorted(buckets.items()):
        nets = [r["simulated_net_pnl"] for r in rows if r.get("simulated_net_pnl") is not None]
        n = len(nets)
        wins = sum(1 for x in nets if x > 0)
        out[k] = {
            "trades": n,
            "win_rate": round(100.0 * wins / n, 1) if n else None,
            "net_pnl": round(sum(nets), 2) if nets else 0.0,
            "average_pnl": round(sum(nets) / n, 2) if n else None,
        }
    return out


def _persist(db: Session, reco_id: int, sim: dict) -> None:
    existing = db.execute(
        select(RecommendationExitSimulation)
        .where(RecommendationExitSimulation.recommendation_id == reco_id)
        .where(RecommendationExitSimulation.exit_policy == sim["exit_policy"])
        .where(RecommendationExitSimulation.exit_policy_version == sim["exit_policy_version"])
    ).scalar_one_or_none()
    if existing:
        # Reproducible refresh of additive sim — never touches outcomes.
        existing.simulated_exit_at = sim.get("simulated_exit_at")
        existing.simulated_exit_rate = sim.get("simulated_exit_rate")
        existing.simulated_exit_reason = sim.get("simulated_exit_reason")
        existing.simulated_gross_pnl = sim.get("simulated_gross_pnl")
        existing.simulated_costs = sim.get("simulated_costs")
        existing.simulated_net_pnl = sim.get("simulated_net_pnl")
        existing.holding_minutes = sim.get("holding_minutes")
        existing.data_completeness = sim.get("data_completeness")
        return
    db.add(
        RecommendationExitSimulation(
            recommendation_id=reco_id,
            exit_policy=sim["exit_policy"],
            exit_policy_version=sim["exit_policy_version"],
            simulated_exit_at=sim.get("simulated_exit_at"),
            simulated_exit_rate=sim.get("simulated_exit_rate"),
            simulated_exit_reason=sim.get("simulated_exit_reason"),
            simulated_gross_pnl=sim.get("simulated_gross_pnl"),
            simulated_costs=sim.get("simulated_costs"),
            simulated_net_pnl=sim.get("simulated_net_pnl"),
            holding_minutes=sim.get("holding_minutes"),
            data_completeness=sim.get("data_completeness"),
        )
    )


def rule_activation_at(db: Session) -> Optional[datetime]:
    """Observational activation of first-profit paper policy (optional override)."""
    settings = get_settings()
    raw = getattr(settings, "paper_first_profit_rule_activated_at", None)
    if raw:
        try:
            return _aware(datetime.fromisoformat(str(raw).replace("Z", "+00:00")))
        except ValueError:
            return None
    # Best-effort: first additive first-net-profit simulation timestamp.
    row = db.execute(
        select(RecommendationExitSimulation.created_at)
        .where(RecommendationExitSimulation.exit_policy == EXIT_FIRST_NET_PROFIT)
        .order_by(RecommendationExitSimulation.created_at.asc())
        .limit(1)
    ).first()
    if row and row[0]:
        return _aware(row[0])
    return None


def compare_exit_strategies(
    db: Session,
    *,
    persist: bool = True,
    max_horizon_hours: float = _MAX_HORIZON_HOURS,
) -> dict:
    """Compare fixed 1d horizon vs first-net-profit using stored snapshots."""
    cfg = cost_config()

    recs = db.execute(
        select(Recommendation).order_by(Recommendation.created_at.asc())
    ).scalars().all()

    eligible = 0
    excluded: list[dict] = []
    old_trades: list[dict] = []
    new_trades: list[dict] = []
    model_versions: set[str] = set()
    period_start: Optional[datetime] = None
    period_end: Optional[datetime] = None

    for reco in recs:
        if reco.direction not in _ACTIONABLE:
            continue
        if reco.spot_price is None:
            excluded.append({"recommendation_id": reco.id, "reason": "missing_spot"})
            continue
        eligible += 1
        model_versions.add(str(reco.model_version or "unknown"))
        created = _aware(reco.created_at)
        period_start = created if period_start is None else min(period_start, created)
        period_end = created if period_end is None else max(period_end, created)

        # Fixed-horizon terminal: prefer stored 1d outcome, else closest snapshot at due.
        outcome_1d = db.execute(
            select(RecommendationOutcome)
            .where(RecommendationOutcome.recommendation_id == reco.id)
            .where(RecommendationOutcome.horizon == "1d")
        ).scalar_one_or_none()

        due = horizon_due_time(created, "1d")
        terminal_mid = None
        terminal_at = None
        if outcome_1d and outcome_1d.spot_at_evaluation is not None:
            terminal_mid = float(outcome_1d.spot_at_evaluation)
            terminal_at = _aware(outcome_1d.evaluated_at or due)
        else:
            ticks_term = load_price_ticks(
                db, reco.pair or "USDMXN", due, due + timedelta(hours=6)
            )
            if ticks_term:
                terminal_at, terminal_mid = ticks_term[0][0], ticks_term[0][1]

        if terminal_mid is None:
            excluded.append({
                "recommendation_id": reco.id,
                "reason": "no_terminal_1d_price",
            })
            continue

        ticks = load_price_ticks(
            db,
            reco.pair or "USDMXN",
            created + timedelta(seconds=1),
            created + timedelta(hours=max_horizon_hours + 1),
        )
        # Exclude incomplete paths for first-profit comparison (PARTIAL SAMPLE).
        post = [t for t in ticks if _aware(t[0]) > created]
        incomplete = len(post) < _MIN_TICKS_FOR_COMPLETE

        meta = {
            "recommendation_id": reco.id,
            "grade": reco.opportunity_grade,
            "confidence_bucket": _conf_bucket(reco.confidence),
            "direction": reco.direction,
            "regime": reco.regime or "unknown",
            "month": created.strftime("%Y-%m"),
            "model_version": reco.model_version or "unknown",
            "created_at": created,
        }

        old_sim = simulate_fixed_horizon(
            direction=reco.direction,
            entry_at=created,
            spot_mid=float(reco.spot_price),
            terminal_mid=float(terminal_mid),
            terminal_at=terminal_at or due,
            cfg=cfg,
        )
        old_row = {**meta, **old_sim}
        old_trades.append(old_row)

        if incomplete:
            excluded.append({
                "recommendation_id": reco.id,
                "reason": "incomplete_intraday_observations",
                "observation_count": len(post),
                "required": _MIN_TICKS_FOR_COMPLETE,
            })
            # Still keep old method; skip new method for this reco.
            if persist:
                _persist(db, reco.id, old_sim)
            continue

        new_sim = simulate_first_net_profit(
            direction=reco.direction,
            entry_at=created,
            spot_mid=float(reco.spot_price),
            ticks=post,
            cfg=cfg,
            max_horizon_hours=max_horizon_hours,
            terminal_mid=float(terminal_mid),
            terminal_at=terminal_at or due,
        )
        new_row = {**meta, **new_sim}
        new_trades.append(new_row)

        if persist:
            _persist(db, reco.id, old_sim)
            _persist(db, reco.id, new_sim)

    if persist:
        try:
            db.commit()
        except Exception:  # noqa: BLE001
            db.rollback()

    # Align comparison sample: only recs in both (intersection by id).
    old_by_id = {t["recommendation_id"]: t for t in old_trades}
    new_ids = {t["recommendation_id"] for t in new_trades}
    old_aligned = [old_by_id[i] for i in new_ids if i in old_by_id]
    new_aligned = [t for t in new_trades if t["recommendation_id"] in old_by_id]

    old_agg = _aggregate(old_aligned, label="OLD METHOD (fixed-horizon 1d)")
    new_agg = _aggregate(new_aligned, label="NEW METHOD (exit at first net profit)")
    old_agg["eligible_recommendations"] = eligible
    new_agg["eligible_recommendations"] = eligible
    old_agg["compared_trades"] = len(old_aligned)
    new_agg["compared_trades"] = len(new_aligned)

    incomplete_excl = sum(
        1 for e in excluded if e.get("reason") == "incomplete_intraday_observations"
    )
    data_limitation_excl = sum(
        1
        for e in excluded
        if e.get("reason")
        in (
            "incomplete_intraday_observations",
            "no_terminal_1d_price",
            "missing_spot",
        )
    )

    delta_net = None
    delta_wr = None
    delta_avg = None
    delta_dd = None
    if old_aligned and new_aligned:
        delta_net = round(new_agg["net_pnl"] - old_agg["net_pnl"], 2)
        if old_agg["win_rate"] is not None and new_agg["win_rate"] is not None:
            delta_wr = round(new_agg["win_rate"] - old_agg["win_rate"], 1)
        if old_agg["average_pnl"] is not None and new_agg["average_pnl"] is not None:
            delta_avg = round(new_agg["average_pnl"] - old_agg["average_pnl"], 2)
        delta_dd = round(new_agg["maximum_drawdown"] - old_agg["maximum_drawdown"], 2)

    verdict = "inconclusive"
    sample_n = len(new_aligned)
    if sample_n >= 10 and delta_net is not None:
        if delta_net > 0 and (delta_avg or 0) >= 0:
            verdict = "better"
        elif delta_net < 0 and (delta_avg or 0) <= 0:
            verdict = "worse"
        else:
            verdict = "inconclusive"
    elif sample_n < 10:
        verdict = "inconclusive"

    activation = rule_activation_at(db)
    before_new: list[dict] = []
    after_new: list[dict] = []
    if activation:
        for t in new_aligned:
            if t["created_at"] < activation:
                before_new.append(t)
            else:
                after_new.append(t)

    partial = data_limitation_excl > 0 or len(model_versions) > 1 or sample_n == 0
    return {
        "label": "Exit Strategy Comparison",
        "disclaimer": (
            "SIMULATED PAPER PERFORMANCE only — decision support. "
            "Not live trading. First-profit uses stored midpoint observations "
            "after recommendation time; no look-ahead."
        ),
        "sample_completeness": "PARTIAL SAMPLE" if partial else "COMPLETE SAMPLE",
        "incomplete_observation_exclusions": incomplete_excl,
        "eligible_recommendations": eligible,
        "excluded_count": len(excluded),
        "excluded": excluded[:200],
        "compared_trades": sample_n,
        "comparison_period": {
            "start": period_start.isoformat() if period_start else None,
            "end": period_end.isoformat() if period_end else None,
        },
        "costs": {
            "notional_usd": cfg.notional_usd,
            "entry_cost_usd": cfg.entry_cost_usd,
            "exit_cost_usd": cfg.exit_cost_usd,
            "half_spread": cfg.half_spread,
            "total_round_trip_usd": cfg.total_cost_usd,
            "identical_for_both_methods": True,
        },
        "old_method": old_agg,
        "new_method": new_agg,
        "difference": {
            "net_pnl": delta_net,
            "win_rate_pp": delta_wr,
            "average_pnl": delta_avg,
            "drawdown": delta_dd,
            "verdict": verdict,
            "sample_size": sample_n,
        },
        "breakdowns": {
            "grade": {
                "old": _breakdown(old_aligned, "grade"),
                "new": _breakdown(new_aligned, "grade"),
            },
            "confidence_bucket": {
                "old": _breakdown(old_aligned, "confidence_bucket"),
                "new": _breakdown(new_aligned, "confidence_bucket"),
            },
            "direction": {
                "old": _breakdown(old_aligned, "direction"),
                "new": _breakdown(new_aligned, "direction"),
            },
            "regime": {
                "old": _breakdown(old_aligned, "regime"),
                "new": _breakdown(new_aligned, "regime"),
            },
            "month": {
                "old": _breakdown(old_aligned, "month"),
                "new": _breakdown(new_aligned, "month"),
            },
            "model_version": {
                "old": _breakdown(old_aligned, "model_version"),
                "new": _breakdown(new_aligned, "model_version"),
            },
        },
        "model_versions_present": sorted(model_versions),
        "since_rule_change": {
            "label": "Observational (not causal)",
            "note": (
                "Compares recommendations before vs after the first paper-position "
                "using the hourly/first-profit policy. Market conditions and "
                "recommendation quality may differ — not a controlled experiment."
            ),
            "activation_at": activation.isoformat() if activation else None,
            "before": _aggregate(before_new, label="before_activation") if activation else None,
            "after": _aggregate(after_new, label="after_activation") if activation else None,
        },
        "exit_policies": {
            "old": EXIT_FIXED_HORIZON,
            "new": EXIT_FIRST_NET_PROFIT,
            "versions": {"old": _FIXED_VERSION, "new": _POLICY_VERSION},
        },
    }
