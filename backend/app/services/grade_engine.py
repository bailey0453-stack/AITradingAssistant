"""Opportunity grade computation (Phase A calibration).

Centralizes letter-grade logic so production scoring, diagnostics, and tests
share one implementation. ``legacy`` mode preserves the pre-Phase-A formula for
before/after comparisons on stored snapshots.

Design goals (v2):
- A/B/C reachable when signal quality + blended confidence justify it.
- Avoid double-counting agreement and confidence (they were the same ratio).
- Soften conflict stacking (weak opposing signals should not always cost −20).
- Directional reads never grade PASS; PASS is reserved for NO_TRADE only.
- A+ capped in uncertain regimes; A/B remain uncommon but possible.
"""

from __future__ import annotations

from collections import Counter
from typing import Literal, Optional

from app.services.market_data import MarketData

GradeVersion = Literal["legacy", "v2"]

# Letter bands — highest threshold first. Kept unchanged in v2 so we do not
# manufacture A grades by lowering the bar; formula fixes lift scores instead.
GRADE_BANDS: tuple[tuple[float, str], ...] = (
    (85.0, "A+"),
    (74.0, "A"),
    (60.0, "B"),
    (46.0, "C"),
    (32.0, "D"),
)

LEGACY_GRADE_BANDS = GRADE_BANDS

# Regimes where A+ is capped to A (headline-driven / uncertain tape).
UNCERTAIN_REGIMES = {"High Volatility", "Political Risk", "Trade War", "Risk Off"}

# v2: only opposing signals with meaningful strength count toward conflict penalty.
_CONFLICT_STRENGTH_FLOOR = 0.25

# Penalty tunables per version.
_LEGACY_CONFLICT_PER = 5.0
_LEGACY_CONFLICT_CAP = 20.0
_V2_CONFLICT_PER = 3.0
_V2_CONFLICT_CAP = 12.0


def _clamp01(value: float) -> float:
    return max(0.0, min(1.0, value))


def _signal_dict(
    signal: dict,
    *,
    confidence_override: Optional[float] = None,
) -> dict:
    """Normalize inputs for grading."""
    out = dict(signal)
    if confidence_override is not None:
        out["confidence"] = confidence_override
    return out


def _conflict_count(signal: dict, version: GradeVersion) -> int:
    conflicts = signal.get("conflicting_signals") or []
    if version == "legacy":
        return len(conflicts)
    return sum(
        1
        for c in conflicts
        if float(c.get("strength") or 0.0) >= _CONFLICT_STRENGTH_FLOOR
    )


def _conflict_penalty(n: int, version: GradeVersion) -> float:
    if version == "legacy":
        return min(_LEGACY_CONFLICT_CAP, n * _LEGACY_CONFLICT_PER)
    return min(_V2_CONFLICT_CAP, n * _V2_CONFLICT_PER)


def _composite_base(
    trade: float,
    agreement: float,
    confidence: float,
    regime_conf: float,
    version: GradeVersion,
) -> float:
    """Weighted blend before penalties.

    Legacy double-counts agreement and signal confidence (same net/total ratio).
    v2 keeps agreement as directional quality, adds confidence as a separate
    term — after Phase 4 blend, confidence reflects historical/regime evidence
    and diverges from raw agreement.
    """
    if version == "legacy":
        return 100.0 * (
            0.4 * trade + 0.3 * agreement + 0.3 * confidence
        )
    return 100.0 * (
        0.45 * trade + 0.25 * agreement + 0.30 * confidence + 0.10 * regime_conf
    )


def compute_opportunity_grade(
    signal: dict,
    regime: dict,
    market: MarketData,
    *,
    confidence_override: Optional[float] = None,
    version: GradeVersion = "v2",
) -> dict:
    """Grade a setup A+..PASS from signal, regime, and market context.

    ``confidence_override`` should be the **final blended** confidence when
    grading after Phase 4 historical intelligence (0..100).
    """
    sig = _signal_dict(signal, confidence_override=confidence_override)
    sb = sig.get("signal_breakdown") or {}
    total = float(sb.get("total_score") or 0.0)
    net = abs(float(sb.get("net_score") or 0.0))
    agreement = (net / total) if total else 0.0
    agreement = _clamp01(agreement)

    confidence = _clamp01(float(sig.get("confidence") or 0.0) / 100.0)
    trade = _clamp01(float(sig.get("trade_score") or 0.0) / 100.0)
    regime_conf = _clamp01(float((regime or {}).get("confidence") or 0.0) / 100.0)

    risk_penalty = {"low": 0.0, "elevated": 8.0, "high": 18.0}.get(
        sig.get("risk_level"), 8.0
    )
    n_conflicts = _conflict_count(sig, version)
    conflict_penalty = _conflict_penalty(n_conflicts, version)

    vix = market.vix if market.vix is not None else 15.0
    vol_penalty = max(0.0, (vix - 18.0)) * 1.5

    base = _composite_base(trade, agreement, confidence, regime_conf, version)
    score = round(base - risk_penalty - conflict_penalty - vol_penalty, 1)

    direction = sig.get("direction")
    if direction == "NO_TRADE":
        grade = "PASS"
    else:
        grade = "D"
        for threshold, letter in GRADE_BANDS:
            if score >= threshold:
                grade = letter
                break
        if grade == "A+" and (regime or {}).get("primary") in UNCERTAIN_REGIMES:
            grade = "A"

    reasons: list[str] = []
    reasons.append(
        f"Signal agreement {round(agreement * 100)}% "
        f"(net {sb.get('net_score')} of {sb.get('total_score')} total weight)."
    )
    if version == "legacy":
        reasons.append(
            f"Confidence {sig.get('confidence')}/100 (signal ratio), "
            f"trade score {sig.get('trade_score')}/100."
        )
    else:
        reasons.append(
            f"Blended confidence {sig.get('confidence')}/100, "
            f"trade score {sig.get('trade_score')}/100, "
            f"regime confidence {round(regime_conf * 100)}%."
        )
    reasons.append(
        f"Regime: {(regime or {}).get('primary')} "
        f"({(regime or {}).get('confidence')}% conf)."
    )
    if risk_penalty:
        reasons.append(f"Risk {sig.get('risk_level')} (-{risk_penalty:g}).")
    if conflict_penalty:
        reasons.append(
            f"{n_conflicts} material conflicting signal(s) (-{conflict_penalty:g})."
        )
    if vol_penalty:
        reasons.append(f"Elevated volatility VIX {vix} (-{round(vol_penalty, 1)}).")
    if direction == "NO_TRADE":
        reasons.append("No directional edge -> PASS.")

    return {
        "grade": grade,
        "score": score,
        "reasons": reasons,
        "grade_version": version,
        "components": {
            "agreement": round(agreement, 3),
            "confidence": round(confidence, 3),
            "trade_score": round(trade, 3),
            "regime_confidence": (regime or {}).get("confidence"),
            "risk_penalty": risk_penalty,
            "conflict_penalty": conflict_penalty,
            "volatility_penalty": round(vol_penalty, 2),
            "composite_base": round(base, 1),
        },
    }


def letter_from_score(score: float, direction: str) -> str:
    """Map a composite score to a letter grade (directional floor at D)."""
    if direction == "NO_TRADE":
        return "PASS"
    for threshold, letter in GRADE_BANDS:
        if score >= threshold:
            return letter
    return "D"


def _confidence_buckets(values: list[float]) -> dict[str, int]:
    """Histogram for 0-50 / 50-70 / 70-85 / 85-100."""
    buckets = Counter({"0-50": 0, "50-70": 0, "70-85": 0, "85-100": 0, "unknown": 0})
    for v in values:
        if v is None:
            buckets["unknown"] += 1
        elif v < 50:
            buckets["0-50"] += 1
        elif v < 70:
            buckets["50-70"] += 1
        elif v < 85:
            buckets["70-85"] += 1
        else:
            buckets["85-100"] += 1
    return dict(buckets)


def grade_distribution(grades: list[str]) -> dict[str, int]:
    order = ["A+", "A", "B", "C", "D", "PASS", "unknown"]
    counts = Counter(grades)
    return {g: counts.get(g, 0) for g in order if counts.get(g, 0)}


def replay_grade_from_snapshot(
    row,
    market: MarketData | None = None,
    *,
    version: GradeVersion = "v2",
    use_blended_confidence: bool = False,
) -> dict | None:
    """Recompute grade from a stored ``AnalysisSnapshot`` row."""
    if not row or not row.signal_breakdown:
        return None
    ms = market
    if ms is None and getattr(row, "market_snapshot", None) is not None:
        snap = row.market_snapshot
        from app.services.market_data import MarketData as MD

        ms = MD(
            usdmxn=snap.usdmxn,
            dxy=snap.dxy,
            us2y=snap.us2y,
            us10y=snap.us10y,
            oil=snap.oil,
            gold=snap.gold,
            sp_futures=snap.sp_futures,
            vix=snap.vix,
        )
    if ms is None:
        from app.services.market_data import MarketData as MD

        ms = MD(vix=15.0)

    cb = row.confidence_breakdown or {}
    inputs = cb.get("inputs") or {}
    signal_conf = inputs.get("current_signals")
    blended_conf = row.confidence

    conf_for_grade = blended_conf if use_blended_confidence else signal_conf
    if conf_for_grade is None:
        conf_for_grade = blended_conf

    signal = {
        "direction": row.direction,
        "trade_score": row.trade_score,
        "confidence": conf_for_grade,
        "risk_level": row.risk_level,
        "conflicting_signals": row.conflicting_signals or [],
        "signal_breakdown": row.signal_breakdown,
    }
    return compute_opportunity_grade(
        signal,
        row.market_regime or {},
        ms,
        version=version,
    )
