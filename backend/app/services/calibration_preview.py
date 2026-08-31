"""Branch-only calibration preview for FX recommendation quality.

This module does not change trading decisions.  It evaluates stored outcomes
using executable net P/L rather than directional correctness and previews a
conservative grade/confidence calibration before production rules are changed.
"""

from __future__ import annotations

from collections import defaultdict
from typing import Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Recommendation, RecommendationOutcome

HORIZONS = ("1h", "4h", "end_of_day")
MIN_GRADE_SAMPLES = 8


def _mean(values: list[float]) -> Optional[float]:
    vals = [float(v) for v in values if v is not None]
    return round(sum(vals) / len(vals), 2) if vals else None


def _pct(n: int, d: int) -> Optional[float]:
    return round(100.0 * n / d, 1) if d else None


def _execution_win(outcome: RecommendationOutcome) -> Optional[bool]:
    if not outcome.actionable or outcome.net_pnl_usd is None:
        return None
    return float(outcome.net_pnl_usd) > 0.0


def _grade_cap(grade: str, samples: int, execution_win_rate: Optional[float], avg_pnl: Optional[float]) -> str:
    """Conservative proposed cap for high grades based on demonstrated economics."""
    if grade not in {"A+", "A", "B"}:
        return grade
    if samples < MIN_GRADE_SAMPLES:
        return "B" if grade in {"A+", "A"} else grade
    if execution_win_rate is None or avg_pnl is None:
        return "B" if grade in {"A+", "A"} else grade
    if execution_win_rate < 55.0 or avg_pnl <= 0:
        return "B" if grade in {"A+", "A"} else "C"
    if grade == "A+" and execution_win_rate < 65.0:
        return "A"
    return grade


def _confidence_bucket(conf: Optional[float]) -> str:
    if conf is None:
        return "unknown"
    if conf < 50:
        return "0-50"
    if conf < 70:
        return "50-70"
    if conf < 85:
        return "70-85"
    return "85-100"


def build_calibration_preview(db: Session, max_rows: int = 20000) -> dict:
    pairs = db.execute(
        select(RecommendationOutcome, Recommendation)
        .join(Recommendation, RecommendationOutcome.recommendation_id == Recommendation.id)
        .where(RecommendationOutcome.horizon.in_(HORIZONS))
        .order_by(RecommendationOutcome.evaluated_at.desc())
        .limit(max_rows)
    ).all()

    actionable = [(o, r) for o, r in pairs if o.actionable and o.net_pnl_usd is not None]
    dir_wins = sum(1 for o, _ in actionable if o.direction_correct)
    exec_wins = sum(1 for o, _ in actionable if _execution_win(o))
    nets = [float(o.net_pnl_usd) for o, _ in actionable]

    by_horizon: dict[str, list] = defaultdict(list)
    by_grade: dict[str, list] = defaultdict(list)
    by_conf: dict[str, list] = defaultdict(list)
    for o, r in actionable:
        by_horizon[o.horizon].append((o, r))
        by_grade[r.opportunity_grade or "n/a"].append((o, r))
        by_conf[_confidence_bucket(r.confidence)].append((o, r))

    def block(rows: list) -> dict:
        d = len(rows)
        return {
            "samples": d,
            "directional_win_rate": _pct(sum(1 for o, _ in rows if o.direction_correct), d),
            "execution_win_rate": _pct(sum(1 for o, _ in rows if _execution_win(o)), d),
            "avg_net_pnl_usd": _mean([o.net_pnl_usd for o, _ in rows]),
            "net_pnl_usd": round(sum(float(o.net_pnl_usd or 0) for o, _ in rows), 2),
        }

    grade_stats = {g: block(rows) for g, rows in sorted(by_grade.items())}
    proposed_caps = {}
    for grade, stats in grade_stats.items():
        proposed_caps[grade] = {
            "current_grade": grade,
            "proposed_max_grade": _grade_cap(
                grade,
                int(stats["samples"]),
                stats["execution_win_rate"],
                stats["avg_net_pnl_usd"],
            ),
            **stats,
        }

    # Count existing A/A+ evaluated outcomes that would not qualify for their
    # current grade under the execution-based cap. This is a calibration preview,
    # not a retrospective rewrite of stored recommendations.
    downgraded = 0
    high_grade = 0
    for o, r in actionable:
        grade = r.opportunity_grade or "n/a"
        if grade not in {"A", "A+"}:
            continue
        high_grade += 1
        cap = proposed_caps.get(grade, {}).get("proposed_max_grade", grade)
        if cap != grade:
            downgraded += 1

    return {
        "mode": "preview_only",
        "basis": "stored executable paper-hedge outcomes after costs",
        "horizons": list(HORIZONS),
        "actionable_outcomes": len(actionable),
        "baseline": {
            "directional_win_rate": _pct(dir_wins, len(actionable)),
            "execution_win_rate": _pct(exec_wins, len(actionable)),
            "difference_points": round((_pct(dir_wins, len(actionable)) or 0) - (_pct(exec_wins, len(actionable)) or 0), 1) if actionable else None,
            "avg_net_pnl_usd": _mean(nets),
            "net_pnl_usd": round(sum(nets), 2) if nets else 0.0,
        },
        "by_horizon": {h: block(by_horizon.get(h, [])) for h in HORIZONS},
        "by_grade": proposed_caps,
        "by_confidence": {k: block(v) for k, v in sorted(by_conf.items())},
        "proposed_high_grade_rule": {
            "minimum_samples": MIN_GRADE_SAMPLES,
            "A_requires": "execution win rate >=55% and positive average net P/L",
            "A_plus_requires": "execution win rate >=65% and positive average net P/L",
            "high_grade_outcomes": high_grade,
            "high_grade_outcomes_downgraded": downgraded,
            "downgrade_rate": _pct(downgraded, high_grade),
        },
    }
