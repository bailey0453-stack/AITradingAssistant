"""Phase 5.3 Decision Quality Engine.

Helps the assistant decide not only *direction* but whether a trade is worth
taking now versus waiting. Built on top of the Research Lab and Paper Hedge
Performance: every read here is over already-stored recommendations / scored
outcomes (cheap) — nothing is evaluated on load.

Key outputs:
- ``trade_quality_score`` / ``trade_quality_label`` — decision quality, separate
  from model confidence.
- reward/risk, minimum required win rate, and expected value (deducting the
  $40 round-trip paper cost on $100k notional).
- ``should_trade_now`` plus a plain-English reason to wait / reason to trade.
- model track record for *similar* past recommendations.
- selective-trading analysis ("if we only traded the top X% ...").

Paper figures are **SIMULATED model evaluation only** — never a real trade.
"""

from __future__ import annotations

from typing import Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import MarketSnapshot, Recommendation, RecommendationOutcome
from app.services.recommendation_evaluator import (
    PAPER_NOTIONAL_USD,
    PAPER_TOTAL_COST_USD,
)

PRIMARY_HORIZON = "1d"
_ACTIONABLE = {"BUY_USD", "SELL_USD"}
_GRADE_RANK = {"A+": 6, "A": 5, "B": 4, "C": 3, "D": 2, "PASS": 1}
_MIN_SIMILAR = 5  # minimum sample before "similar track record" is trustworthy

# Trade-quality component weights (renormalized over whichever inputs exist).
_WEIGHTS = {
    "signal_strength": 0.22,
    "historical_evidence": 0.18,
    "reward_risk": 0.20,
    "event_risk": 0.12,
    "volatility_fit": 0.08,
    "model_track_record": 0.10,
    "paper_hedge_similar": 0.10,
}

_QUALITY_BANDS = [(80.0, "Excellent"), (65.0, "Good"), (50.0, "Marginal"), (35.0, "Poor")]


# --- small helpers ----------------------------------------------------------
def _clamp(v: float, lo: float = 0.0, hi: float = 100.0) -> float:
    return max(lo, min(hi, v))


def _blend(components: dict) -> Optional[float]:
    """Weighted blend over present components only (missing inputs renormalize)."""
    num = den = 0.0
    for name, weight in _WEIGHTS.items():
        score = components.get(name)
        if score is None:
            continue
        num += weight * score
        den += weight
    return round(num / den, 1) if den > 0 else None


def _label_for(score: Optional[float], actionable: bool) -> str:
    if not actionable or score is None:
        return "Wait"
    for threshold, label in _QUALITY_BANDS:
        if score >= threshold:
            return label
    return "Wait"


# --- reward / risk + expected value ----------------------------------------
def reward_risk(direction: str, entry, target, stop) -> dict:
    """Reward-to-target, risk-to-stop, R/R ratio, breakeven win rate."""
    out = {
        "reward_to_target": None,
        "risk_to_stop": None,
        "reward_risk_ratio": None,
        "minimum_required_win_rate": None,
    }
    if direction not in _ACTIONABLE or entry in (None, 0) or target is None or stop is None:
        return out
    if direction == "BUY_USD":
        reward, risk = target - entry, entry - stop
    else:  # SELL_USD
        reward, risk = entry - target, stop - entry
    out["reward_to_target"] = round(reward, 6)
    out["risk_to_stop"] = round(risk, 6)
    if risk and risk > 0 and reward is not None and reward > 0:
        rr = reward / risk
        out["reward_risk_ratio"] = round(rr, 2)
        out["minimum_required_win_rate"] = round(risk / (reward + risk) * 100, 1)
    return out


def expected_value(
    direction: str, entry, target, stop,
    p_target_pct: Optional[float], p_stop_pct: Optional[float],
) -> dict:
    """Expected value on $100k notional, net of the $40 round-trip paper cost.

    ``p_target_pct`` is the probability (0-100) of reaching the target;
    ``p_stop_pct`` the probability of hitting the stop. When the stop
    probability is unknown it is taken as the complement of the win probability.
    """
    out = {
        "notional_usd": PAPER_NOTIONAL_USD,
        "round_trip_cost_usd": PAPER_TOTAL_COST_USD,
        "p_target": p_target_pct,
        "p_stop": p_stop_pct,
        "reward_pct": None,
        "risk_pct": None,
        "gross_expected_pct": None,
        "expected_value_usd": None,
        "basis": None,
    }
    if direction not in _ACTIONABLE or entry in (None, 0) or target is None or stop is None:
        out["basis"] = "No actionable trade plan (missing direction / target / stop)."
        return out
    if p_target_pct is None:
        out["basis"] = "Insufficient probability evidence to estimate expected value."
        return out

    reward_pct = abs(target - entry) / entry * 100
    risk_pct = abs(entry - stop) / entry * 100
    p_win = _clamp(p_target_pct, 0, 100) / 100.0
    p_loss = (_clamp(p_stop_pct, 0, 100) / 100.0) if p_stop_pct is not None else (1.0 - p_win)

    gross_pct = p_win * reward_pct - p_loss * risk_pct
    ev_usd = PAPER_NOTIONAL_USD * gross_pct / 100.0 - PAPER_TOTAL_COST_USD
    out.update({
        "reward_pct": round(reward_pct, 4),
        "risk_pct": round(risk_pct, 4),
        "gross_expected_pct": round(gross_pct, 4),
        "expected_value_usd": round(ev_usd, 2),
        "basis": (
            f"EV = ${PAPER_NOTIONAL_USD:,.0f} x ({round(p_win*100,1)}% x {round(reward_pct,3)}% "
            f"- {round(p_loss*100,1)}% x {round(risk_pct,3)}%) - ${PAPER_TOTAL_COST_USD:,.0f} round-trip cost."
        ),
    })
    return out


# --- similar-recommendation track record ------------------------------------
def _rate(vals) -> Optional[float]:
    vals = [v for v in vals if v is not None]
    return round(100 * sum(1 for v in vals if v) / len(vals), 1) if vals else None


def _mean(vals) -> Optional[float]:
    vals = [v for v in vals if v is not None]
    return round(sum(vals) / len(vals), 2) if vals else None


def _primary_pairs(db: Session, limit: int = 50000):
    rows = db.execute(
        select(RecommendationOutcome, Recommendation)
        .join(Recommendation, RecommendationOutcome.recommendation_id == Recommendation.id)
        .where(RecommendationOutcome.horizon == PRIMARY_HORIZON)
        .order_by(Recommendation.created_at.asc())
        .limit(limit)
    ).all()
    return rows


def _track_block(pairs) -> dict:
    outs = [o for o, _ in pairs]
    actionable = [o for o in outs if o.actionable]
    return {
        "similar_recommendation_count": len(outs),
        "similar_win_rate": _rate([o.direction_correct for o in outs]),
        "similar_avg_pnl": _mean([o.net_pnl_usd for o in actionable]),
        "similar_target_hit_rate": _rate([o.target_hit for o in outs]),
        "similar_stop_hit_rate": _rate([o.stop_hit for o in outs]),
    }


def similar_track_record(
    db: Session, direction: Optional[str], grade: Optional[str],
    regime: Optional[str] = None, min_samples: int = _MIN_SIMILAR,
) -> dict:
    """Track record of past recommendations similar to the current one.

    Match priority: same direction + same grade. If that subset is too small,
    relax to direction-only and flag the relaxation. Clearly reports when there
    is not yet enough history to be reliable.
    """
    pairs = _primary_pairs(db)
    same_dir = [(o, r) for o, r in pairs if r.direction == direction]
    same_dir_grade = [(o, r) for o, r in same_dir if (r.opportunity_grade or None) == grade]

    used = same_dir_grade
    match_basis = "direction + grade"
    relaxed = False
    if len(same_dir_grade) < min_samples and len(same_dir) > len(same_dir_grade):
        used = same_dir
        match_basis = "direction only"
        relaxed = True

    block = _track_block(used)
    enough = block["similar_recommendation_count"] >= min_samples
    block.update({
        "match_basis": match_basis,
        "relaxed_match": relaxed,
        "enough_history": enough,
        "min_samples": min_samples,
        "note": (
            "Sufficient similar history to be meaningful."
            if enough else
            f"Not enough similar history yet — only {block['similar_recommendation_count']} "
            f"comparable evaluated recommendation(s) so far (need {min_samples}); "
            "rates shown are provisional and excluded from the quality score."
        ),
    })
    return block


# --- trade quality + decision ----------------------------------------------
def _wait_block(*, should_trade: bool, actionable: bool, grade: Optional[str],
                grade_ok: bool, rr: dict, label: str, rec: dict, track: dict) -> dict:
    rrr = rr.get("reward_risk_ratio")
    entry, target, stop = rec.get("entry"), rec.get("target"), rec.get("stop")
    watch: list[str] = []
    if rec.get("high_impact_event_count"):
        watch.append("High-impact economic events are imminent — reassess after the release.")
    if entry is not None and stop is not None:
        watch.append(f"A pullback toward the stop near {round(stop, 4)} would invalidate the setup.")
    if track.get("enough_history") and track.get("similar_win_rate") is not None:
        watch.append(
            f"Similar setups historically won {track['similar_win_rate']}% of the time "
            f"({track['similar_recommendation_count']} samples)."
        )
    if not watch:
        watch.append("Confirmation from DXY, US yields, and momentum aligning with the signal.")

    if should_trade:
        return {
            "reason_to_trade": (
                f"Grade {grade} with reward/risk {rrr} (>= 1.0) and a positive quality "
                f"profile ({label}). The edge justifies acting now with disciplined sizing."
            ),
            "reason_to_wait": None,
            "better_entry_conditions": [],
            "what_to_watch_next": watch,
        }

    reasons: list[str] = []
    if not actionable:
        reasons.append("Signal is NO_TRADE / PASS — there is no directional edge to act on.")
    else:
        if not grade_ok:
            reasons.append(
                f"Opportunity grade {grade or 'n/a'} is below B — setup quality is low."
            )
        if rrr is None:
            reasons.append("No valid reward/risk (missing or inverted target/stop).")
        elif rrr < 1.0:
            reasons.append(f"Reward/risk {rrr} is below 1.0 — risk outweighs reward.")
        if rec.get("high_impact_event_count"):
            reasons.append("High-impact events are imminent; entering now adds event risk.")
    reason_to_wait = " ".join(reasons) or "Setup does not meet the decision-quality threshold."

    better = []
    if actionable and not grade_ok:
        better.append("Wait for a grade B or better setup with confirming signals.")
    if rrr is None or rrr < 1.5:
        better.append("Require reward/risk of at least 1.5 before committing.")
    if rec.get("high_impact_event_count"):
        better.append("Let the imminent high-impact event clear, then reassess.")
    if entry is not None:
        better.append(f"Prefer a more favorable entry than {round(entry, 4)} to widen the edge.")
    if not better:
        better.append("Wait for stronger signal agreement and historical confirmation.")

    return {
        "reason_to_wait": reason_to_wait,
        "reason_to_trade": None,
        "better_entry_conditions": better,
        "what_to_watch_next": watch,
    }


def assess_recommendation(db: Session, rec: dict) -> dict:
    """Full decision-quality assessment for one normalized recommendation dict."""
    direction = rec.get("direction")
    actionable = direction in _ACTIONABLE
    grade = rec.get("grade")
    # A PASS grade means the model is standing aside — never tradeable, even if a
    # direction leaked through (keeps the engine conservative & self-consistent).
    tradeable = actionable and grade != "PASS"

    rr = reward_risk(direction, rec.get("entry"), rec.get("target"), rec.get("stop"))
    rrr = rr.get("reward_risk_ratio")
    track = similar_track_record(db, direction, grade, rec.get("regime"))

    # --- component sub-scores (each 0-100 or None) ---
    signal = rec.get("trade_score")
    if signal is None:
        signal = rec.get("confidence")
    signal = _clamp(signal) if signal is not None else None

    sim = rec.get("best_similarity")
    hwr = rec.get("hist_win_rate")
    hist_parts = []
    if sim is not None:
        hist_parts.append(_clamp(sim * 100.0))
    if hwr is not None:
        hist_parts.append(_clamp(hwr))
    hist_score = round(sum(hist_parts) / len(hist_parts), 1) if hist_parts else None

    vix = rec.get("vix")
    vol_fit = _clamp(100.0 - max(0.0, (vix - 12.0)) * 5.0) if vix is not None else None

    ev_count = rec.get("high_impact_event_count")
    event_score = _clamp(100.0 - 25.0 * ev_count) if ev_count is not None else None

    if rrr is None:
        rr_score = 0.0 if (actionable and rr.get("reward_to_target") is not None) else None
    else:
        rr_score = _clamp(rrr / 2.0 * 100.0)

    mtr = track["similar_win_rate"] if track["enough_history"] else None
    php = None
    if track["enough_history"] and track["similar_avg_pnl"] is not None:
        php = _clamp(50.0 + track["similar_avg_pnl"] / 10.0)

    components = {
        "signal_strength": signal,
        "historical_evidence": hist_score,
        "reward_risk": rr_score,
        "event_risk": event_score,
        "volatility_fit": vol_fit,
        "model_track_record": mtr,
        "paper_hedge_similar": php,
    }
    score = _blend(components)

    grade_ok = _GRADE_RANK.get(grade or "", 0) >= _GRADE_RANK["B"]
    rr_ok = rrr is not None and rrr >= 1.0
    should_trade = bool(tradeable and grade_ok and rr_ok)

    label = _label_for(score, tradeable)
    # Keep label coherent with the gate: don't advertise a top label while waiting.
    if tradeable and not should_trade and label in ("Excellent", "Good"):
        label = "Marginal"
    if not tradeable:
        label = "Wait"

    # Expected value: prefer historical probability of target, then similar /
    # historical win rate as a fallback for the win probability. Only ever
    # produce a value for a genuinely tradeable setup (null for PASS/NO_TRADE).
    p_target = rec.get("prob_target")
    if p_target is None and track["enough_history"]:
        p_target = track["similar_win_rate"]
    if p_target is None:
        p_target = rec.get("hist_win_rate")
    ev = expected_value(
        direction if tradeable else "NO_TRADE",
        rec.get("entry"), rec.get("target"), rec.get("stop"),
        p_target, rec.get("prob_stop"),
    )

    wait = _wait_block(
        should_trade=should_trade, actionable=tradeable, grade=grade,
        grade_ok=grade_ok, rr=rr, label=label, rec=rec, track=track,
    )

    return {
        "trade_quality_score": score,
        "trade_quality_label": label,
        "should_trade_now": should_trade,
        "direction": direction,
        "opportunity_grade": grade,
        "components": {k: (round(v, 1) if v is not None else None) for k, v in components.items()},
        "component_weights": _WEIGHTS,
        "reward_risk": rr,
        "expected_value": ev,
        "similar_track_record": track,
        **wait,
    }


# --- normalized extraction from a payload / stored snapshot -----------------
def _count_high_impact(events) -> int:
    return sum(
        1 for e in (events or [])
        if str((e or {}).get("importance", "")).lower() == "high"
    )


def rec_from_payload(payload: dict) -> dict:
    """Normalize an /analysis/usdmxn payload into the decision-engine input."""
    market = payload.get("market") or {}
    hist = payload.get("historical") or {}
    stats = (hist.get("statistics") or {}) if isinstance(hist, dict) else {}
    levels = (payload.get("probabilities") or {}).get("levels") or {}
    ctx = payload.get("context") or {}
    regime = (payload.get("market_regime") or {}).get("primary")
    return {
        "direction": payload.get("direction"),
        "grade": payload.get("opportunity_grade"),
        "trade_score": payload.get("trade_score"),
        "confidence": payload.get("confidence"),
        "entry": payload.get("entry"),
        "target": payload.get("target"),
        "stop": payload.get("stop"),
        "vix": market.get("vix"),
        "best_similarity": hist.get("best_similarity") if isinstance(hist, dict) else None,
        "hist_win_rate": stats.get("win_rate"),
        "hist_sample_size": stats.get("sample_size"),
        "prob_target": levels.get("probability_reaches_target_1"),
        "prob_stop": levels.get("probability_hits_stop"),
        "high_impact_event_count": _count_high_impact(ctx.get("upcoming_events")),
        "regime": regime,
    }


def rec_from_snapshot(db: Session, row) -> dict:
    """Normalize a stored ``AnalysisSnapshot`` into the decision-engine input."""
    hist = row.historical_context or {}
    stats = (hist.get("statistics") or {}) if isinstance(hist, dict) else {}
    levels = (row.probabilities or {}).get("levels") or {}
    regime = (row.market_regime or {}).get("primary") if row.market_regime else None
    vix = None
    if row.market_snapshot_id:
        ms = db.get(MarketSnapshot, row.market_snapshot_id)
        vix = ms.vix if ms else None
    upcoming = (row.calendar_context or {}).get("upcoming") if row.calendar_context else None
    return {
        "direction": row.direction,
        "grade": row.opportunity_grade,
        "trade_score": row.trade_score,
        "confidence": row.confidence,
        "entry": row.entry,
        "target": row.target,
        "stop": row.stop,
        "vix": vix,
        "best_similarity": hist.get("best_similarity") if isinstance(hist, dict) else None,
        "hist_win_rate": stats.get("win_rate"),
        "hist_sample_size": stats.get("sample_size"),
        "prob_target": levels.get("probability_reaches_target_1"),
        "prob_stop": levels.get("probability_hits_stop"),
        "high_impact_event_count": _count_high_impact(upcoming),
        "regime": regime,
    }


# --- selective trading analysis ---------------------------------------------
def _selective_stats(trades: list[dict]) -> dict:
    n = len(trades)
    if not n:
        return {"trades": 0, "win_rate": None, "net_pnl_usd": 0.0, "avg_pnl_usd": None,
                "max_drawdown_usd": 0.0, "return_on_notional_pct": None}
    nets = [t["net"] for t in trades]
    ordered = sorted(trades, key=lambda t: t["created_at"] or 0)
    equity = peak = mdd = 0.0
    for t in ordered:
        equity += t["net"]
        peak = max(peak, equity)
        mdd = max(mdd, peak - equity)
    return {
        "trades": n,
        "win_rate": _rate([t["win"] for t in trades]),
        "net_pnl_usd": round(sum(nets), 2),
        "avg_pnl_usd": round(sum(nets) / n, 2),
        "max_drawdown_usd": round(mdd, 2),
        "return_on_notional_pct": round(sum(nets) / (n * PAPER_NOTIONAL_USD) * 100, 4),
    }


def selective_performance(db: Session, max_outcomes: int = 50000) -> dict:
    """"If we only traded the top X% / best grades / highest confidence, ...?"

    Read-only over scored, actionable 1d outcomes. Works with limited history
    (groups simply report zero trades until enough data exists).
    """
    pairs = _primary_pairs(db, limit=max_outcomes)
    trades: list[dict] = []
    for o, r in pairs:
        if not o.actionable or o.net_pnl_usd is None:
            continue
        score = r.trade_score if r.trade_score is not None else (r.confidence or 0.0)
        trades.append({
            "created_at": r.created_at,
            "score": score,
            "confidence": r.confidence,
            "grade": r.opportunity_grade,
            "net": o.net_pnl_usd,
            "win": o.direction_correct,
        })

    by_score = sorted(trades, key=lambda t: t["score"] or 0.0, reverse=True)

    def top_pct(p: float) -> list[dict]:
        if not by_score:
            return []
        k = max(1, int(round(len(by_score) * p)))
        return by_score[:k]

    def grade_at_least(letter: str) -> list[dict]:
        floor = _GRADE_RANK[letter]
        return [t for t in trades if _GRADE_RANK.get(t["grade"] or "", 0) >= floor]

    def conf_over(threshold: float) -> list[dict]:
        return [t for t in trades if (t["confidence"] or 0.0) > threshold]

    return {
        "label": "SIMULATED PAPER PERFORMANCE",
        "primary_horizon": PRIMARY_HORIZON,
        "notional_usd": PAPER_NOTIONAL_USD,
        "cost_per_trade_usd": PAPER_TOTAL_COST_USD,
        "all_trades": _selective_stats(trades),
        "filters": {
            "top_10pct": _selective_stats(top_pct(0.10)),
            "top_20pct": _selective_stats(top_pct(0.20)),
            "top_30pct": _selective_stats(top_pct(0.30)),
            "grade_A_or_better": _selective_stats(grade_at_least("A")),
            "grade_B_or_better": _selective_stats(grade_at_least("B")),
            "confidence_over_70": _selective_stats(conf_over(70.0)),
            "confidence_over_80": _selective_stats(conf_over(80.0)),
        },
    }
