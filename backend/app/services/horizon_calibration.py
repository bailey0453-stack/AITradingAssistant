"""Empirical calibration for the displayed USD/MXN forecast horizons.

Each horizon learns from its own already-evaluated paper recommendations.  The
calibrator is intentionally conservative:

* direction confidence is updated with a beta-style empirical prior using the
  observed directional hit rate for the same direction/confidence bucket;
* target magnitude may be *shrunk* to the typical realized move for that
  horizon, but is never enlarged by calibration;
* small samples do not change the forecast.

This keeps 1h, 2h, 4h, end-of-day and 24h behavior separate and avoids letting a
longer-horizon move mechanically leak into the short-horizon targets.
"""

from __future__ import annotations

from statistics import median
from typing import Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Recommendation, RecommendationOutcome

_ACTIONABLE = {"BUY_USD", "SELL_USD"}
_DIR_SIGN = {"BUY_USD": 1.0, "SELL_USD": -1.0}

# Display horizon -> evaluator horizon.
HORIZON_KEYS = {
    "1h": "1h",
    "2h": "2h",
    "4h": "4h",
    "end_of_day": "end_of_day",
    "24h": "1d",
}

_CONFIDENCE_BUCKETS = (
    ("0-50", 0.0, 50.0),
    ("50-70", 50.0, 70.0),
    ("70-85", 70.0, 85.0),
    ("85-100", 85.0, 100.01),
)


def _bucket(confidence: Optional[float]) -> tuple[str, float, float]:
    if confidence is None:
        return "unknown", -1.0, -1.0
    value = float(confidence)
    for name, lo, hi in _CONFIDENCE_BUCKETS:
        if lo <= value < hi:
            return name, lo, hi
    return "unknown", -1.0, -1.0


def _median(values: list[float]) -> Optional[float]:
    return float(median(values)) if values else None


def calibrate_horizon(
    db: Session,
    *,
    horizon: str,
    direction: str,
    raw_move_pct: Optional[float],
    raw_confidence: Optional[float],
    min_samples: int = 20,
    max_samples: int = 2000,
    confidence_prior_samples: int = 20,
) -> dict:
    """Calibrate one horizon from its own evaluated recommendation history.

    ``raw_move_pct`` is the current evidence-derived move.  When the sample is
    reliable, its magnitude is capped at the median absolute realized move for
    this exact evaluation horizon.  This is deliberately one-way shrinkage: the
    calibration layer can prevent an over-extended target but cannot manufacture
    a larger one than the underlying historical analog evidence supplied.
    """
    evaluator_horizon = HORIZON_KEYS.get(horizon, horizon)
    bucket, lo, hi = _bucket(raw_confidence)
    result = {
        "method": "horizon_empirical_shrinkage_v1",
        "horizon": horizon,
        "evaluator_horizon": evaluator_horizon,
        "direction": direction,
        "confidence_bucket": bucket,
        "samples": 0,
        "minimum_samples": min_samples,
        "reliable": False,
        "directional_accuracy": None,
        "median_abs_realized_move_pct": None,
        "median_directional_return_pct": None,
        "raw_move_pct": round(float(raw_move_pct), 4) if raw_move_pct is not None else None,
        "calibrated_move_pct": round(float(raw_move_pct), 4) if raw_move_pct is not None else None,
        "raw_confidence": round(float(raw_confidence), 1) if raw_confidence is not None else None,
        "calibrated_confidence": round(float(raw_confidence), 1) if raw_confidence is not None else None,
        "move_shrunk": False,
        "status": "unavailable",
    }
    if direction not in _ACTIONABLE:
        result["status"] = "not_actionable"
        return result

    stmt = (
        select(RecommendationOutcome, Recommendation)
        .join(Recommendation, RecommendationOutcome.recommendation_id == Recommendation.id)
        .where(RecommendationOutcome.horizon == evaluator_horizon)
        .where(Recommendation.direction == direction)
        .where(RecommendationOutcome.return_pct.is_not(None))
        .where(RecommendationOutcome.direction_correct.is_not(None))
        .order_by(RecommendationOutcome.evaluated_at.desc())
        .limit(max_samples)
    )
    if bucket != "unknown":
        stmt = stmt.where(Recommendation.confidence >= lo).where(Recommendation.confidence < hi)

    rows = db.execute(stmt).all()
    n = len(rows)
    result["samples"] = n
    if not rows:
        result["status"] = "collecting"
        return result

    sign = _DIR_SIGN[direction]
    realized = [float(outcome.return_pct) for outcome, _ in rows if outcome.return_pct is not None]
    directional = [sign * value for value in realized]
    abs_moves = [abs(value) for value in realized]
    wins = sum(1 for outcome, _ in rows if outcome.direction_correct is True)
    accuracy = 100.0 * wins / n if n else None
    typical_abs = _median(abs_moves)
    median_directional = _median(directional)

    result.update({
        "directional_accuracy": round(accuracy, 1) if accuracy is not None else None,
        "median_abs_realized_move_pct": round(typical_abs, 4) if typical_abs is not None else None,
        "median_directional_return_pct": round(median_directional, 4) if median_directional is not None else None,
    })

    reliable = n >= min_samples
    result["reliable"] = reliable
    result["status"] = "measured" if reliable else "provisional"
    if not reliable:
        return result

    # Empirical-Bayes confidence: current model confidence acts as a modest prior,
    # while this horizon's observed hit rate increasingly dominates as n grows.
    if raw_confidence is not None:
        prior_p = max(0.0, min(1.0, float(raw_confidence) / 100.0))
        posterior = (wins + confidence_prior_samples * prior_p) / (n + confidence_prior_samples)
        result["calibrated_confidence"] = round(100.0 * posterior, 1)

    # Conservative magnitude calibration.  We only shrink and never expand.
    if raw_move_pct is not None and typical_abs is not None and typical_abs > 0:
        raw = float(raw_move_pct)
        raw_mag = abs(raw)
        calibrated_mag = min(raw_mag, typical_abs)
        calibrated = calibrated_mag if raw >= 0 else -calibrated_mag
        result["calibrated_move_pct"] = round(calibrated, 4)
        result["move_shrunk"] = calibrated_mag + 1e-12 < raw_mag
        result["magnitude_cap_pct"] = round(typical_abs, 4)
        result["shrink_ratio"] = round(calibrated_mag / raw_mag, 4) if raw_mag else 1.0

    return result


def calibration_summary(
    db: Session,
    *,
    direction: str,
    raw_moves: dict[str, Optional[float]],
    raw_confidences: dict[str, Optional[float]],
) -> dict[str, dict]:
    """Return independent calibration blocks for all forecast horizons."""
    return {
        horizon: calibrate_horizon(
            db,
            horizon=horizon,
            direction=direction,
            raw_move_pct=raw_moves.get(horizon),
            raw_confidence=raw_confidences.get(horizon),
        )
        for horizon in HORIZON_KEYS
    }
