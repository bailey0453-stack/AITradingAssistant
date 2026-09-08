"""Measured calibration summary for the current dashboard recommendation.

Reads already-evaluated paper recommendation outcomes. It never evaluates a new
recommendation, changes model weights, or mixes simulated outcomes with real
trades. The goal is to translate an internal confidence score into an observed
historical track record for the same confidence bucket and horizon.
"""

from __future__ import annotations

from typing import Optional

from sqlalchemy import select

from app.database import SessionLocal
from app.models import Recommendation, RecommendationOutcome

_CONFIDENCE_BUCKETS = [
    ("0-50", 0.0, 50.0),
    ("50-70", 50.0, 70.0),
    ("70-85", 70.0, 85.0),
    ("85-100", 85.0, 100.01),
]


def _bucket(confidence: Optional[float]) -> tuple[str, float, float]:
    if confidence is None:
        return "unknown", -1.0, -1.0
    value = float(confidence)
    for name, lo, hi in _CONFIDENCE_BUCKETS:
        if lo <= value < hi:
            return name, lo, hi
    return "unknown", -1.0, -1.0


def _rate(values) -> Optional[float]:
    vals = [v for v in values if v is not None]
    if not vals:
        return None
    return round(100.0 * sum(1 for v in vals if v) / len(vals), 1)


def _mean(values, digits: int = 4) -> Optional[float]:
    vals = [float(v) for v in values if v is not None]
    if not vals:
        return None
    return round(sum(vals) / len(vals), digits)


def calibration_for_confidence(
    confidence: Optional[float], *, horizon: str = "4h", min_reliable_samples: int = 20
) -> dict:
    """Return observed performance for the current confidence bucket/horizon.

    ``avg_abs_target_error`` is the mean absolute distance in MXN between the
    stored primary target and the observed rate at the requested horizon. Older
    recommendations without a target are excluded from that error statistic.
    """
    name, lo, hi = _bucket(confidence)
    base = {
        "horizon": horizon,
        "confidence_bucket": name,
        "current_confidence": round(float(confidence), 1) if confidence is not None else None,
        "samples": 0,
        "directional_accuracy": None,
        "target_hit_rate": None,
        "avg_abs_target_error": None,
        "mean_model_confidence": None,
        "reliable": False,
        "min_reliable_samples": min_reliable_samples,
        "status": "unavailable" if name == "unknown" else "collecting",
    }
    if name == "unknown":
        return base

    db = SessionLocal()
    try:
        rows = db.execute(
            select(RecommendationOutcome, Recommendation)
            .join(Recommendation, RecommendationOutcome.recommendation_id == Recommendation.id)
            .where(RecommendationOutcome.horizon == horizon)
            .where(Recommendation.confidence.is_not(None))
            .where(Recommendation.confidence >= lo)
            .where(Recommendation.confidence < hi)
            .order_by(RecommendationOutcome.evaluated_at.desc())
            .limit(5000)
        ).all()

        samples = len(rows)
        errors = [
            abs(float(o.spot_at_evaluation) - float(r.target))
            for o, r in rows
            if o.spot_at_evaluation is not None and r.target is not None
        ]
        reliable = samples >= min_reliable_samples
        base.update({
            "samples": samples,
            "directional_accuracy": _rate([o.direction_correct for o, _ in rows]),
            "target_hit_rate": _rate([o.target_hit for o, _ in rows]),
            "avg_abs_target_error": _mean(errors, 4),
            "mean_model_confidence": _mean([r.confidence for _, r in rows], 1),
            "reliable": reliable,
            "status": "measured" if reliable else ("provisional" if samples else "collecting"),
        })
        return base
    finally:
        db.close()


def calibration_text(cal: dict) -> str:
    """Compact dashboard sentence; never overstates a small sample."""
    n = int(cal.get("samples") or 0)
    bucket = cal.get("confidence_bucket") or "unknown"
    horizon = cal.get("horizon") or "4h"
    if n == 0:
        return f"Calibration: no evaluated {horizon} forecasts yet in confidence bucket {bucket}."

    accuracy = cal.get("directional_accuracy")
    hit = cal.get("target_hit_rate")
    err = cal.get("avg_abs_target_error")
    prefix = "Measured history" if cal.get("reliable") else "Provisional history"
    parts = [f"{prefix} for {bucket} confidence at {horizon}: direction {accuracy if accuracy is not None else 'N/A'}% (n={n})"]
    if hit is not None:
        parts.append(f"target hit {hit}%")
    if err is not None:
        parts.append(f"avg target error {err:.4f} MXN")
    if not cal.get("reliable"):
        parts.append(f"need {cal.get('min_reliable_samples', 20)} samples for a reliable calibration")
    return "; ".join(parts) + "."
