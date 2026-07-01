"""Before/after grade calibration diagnostics over stored snapshots."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session, joinedload

from app.models import AnalysisSnapshot
from app.services.grade_engine import (
    _confidence_buckets,
    grade_distribution,
    replay_grade_from_snapshot,
)


def grade_calibration_report(db: Session, limit: int = 100) -> dict:
    """Compare legacy pre-blend grading vs Phase A v2 post-blend grading.

    **Before (legacy):** original formula, signal-only confidence, full conflict
    list, grade computed before historical blend (simulated via
    ``current_signals`` from ``confidence_breakdown``).

    **After (v2):** de-duplicated composite, material conflicts only, final
    stored blended confidence.
    """
    rows = db.execute(
        select(AnalysisSnapshot)
        .options(joinedload(AnalysisSnapshot.market_snapshot))
        .order_by(AnalysisSnapshot.created_at.desc())
        .limit(limit)
    ).scalars().all()

    stored_grades: list[str] = []
    legacy_grades: list[str] = []
    v2_grades: list[str] = []
    stored_conf: list[float] = []
    signal_conf: list[float] = []
    blended_conf: list[float] = []

    shifts: list[dict] = []
    samples = 0

    for row in rows:
        samples += 1
        stored_grades.append(row.opportunity_grade or "unknown")

        cb = row.confidence_breakdown or {}
        inputs = cb.get("inputs") or {}
        sig_c = inputs.get("current_signals")
        blend_c = row.confidence

        if blend_c is not None:
            stored_conf.append(float(blend_c))
            blended_conf.append(float(blend_c))
        if sig_c is not None:
            signal_conf.append(float(sig_c))

        legacy = replay_grade_from_snapshot(
            row,
            version="legacy",
            use_blended_confidence=False,
        )
        v2 = replay_grade_from_snapshot(
            row,
            version="v2",
            use_blended_confidence=True,
        )

        leg_g = legacy["grade"] if legacy else "unknown"
        v2_g = v2["grade"] if v2 else "unknown"
        legacy_grades.append(leg_g)
        v2_grades.append(v2_g)

        if leg_g != v2_g:
            shifts.append({
                "analysis_id": row.id,
                "created_at": row.created_at.isoformat() if row.created_at else None,
                "direction": row.direction,
                "legacy_grade": leg_g,
                "v2_grade": v2_g,
                "stored_grade": row.opportunity_grade,
                "signal_confidence": sig_c,
                "blended_confidence": blend_c,
                "legacy_score": legacy.get("score") if legacy else None,
                "v2_score": v2.get("score") if v2 else None,
            })

    return {
        "samples": samples,
        "limit": limit,
        "description": (
            "Before = legacy formula + signal confidence (pre-blend). "
            "After = Phase A v2 formula + stored blended confidence."
        ),
        "grade_distribution": {
            "stored_in_db": grade_distribution(stored_grades),
            "before_legacy_replay": grade_distribution(legacy_grades),
            "after_v2_replay": grade_distribution(v2_grades),
        },
        "confidence_distribution": {
            "stored_headline": _confidence_buckets(stored_conf),
            "signal_pre_blend": _confidence_buckets(signal_conf),
            "blended_post_blend": _confidence_buckets(blended_conf),
        },
        "grade_shifts_legacy_to_v2": {
            "count": len(shifts),
            "examples": shifts[:15],
        },
        "notes": [
            "PASS is assigned only when direction is NO_TRADE.",
            "Directional grades floor at D; they never become PASS.",
            "A/B remain gated by unchanged band thresholds (85/74/60/46/32).",
        ],
    }
