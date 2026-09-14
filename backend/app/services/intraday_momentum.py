"""Tactical USD/MXN momentum derived from fixed intraday windows.

The context builder records 5m/15m/30m/60m changes from durable market
snapshots. This module compresses those windows into one signed change for the
existing signal-weighting engine while preserving the full diagnostics for the
2-4 hour tactical card.
"""

from __future__ import annotations

from typing import Any

_WINDOW_WEIGHTS = {"5m": 0.40, "15m": 0.30, "30m": 0.20, "60m": 0.10}
# A move of this size is considered full strength for each horizon. The shorter
# windows deliberately react before a 2-3 cent opportunity has already passed.
_WINDOW_NORMS = {"5m": 0.012, "15m": 0.020, "30m": 0.030, "60m": 0.050}


def _f(value: Any) -> float | None:
    try:
        return None if value is None else float(value)
    except (TypeError, ValueError):
        return None


def aggregate(momentum: dict | None) -> dict | None:
    """Return a compatibility momentum object plus tactical diagnostics.

    ``change`` is mapped back onto the legacy 0.05 full-strength scale so the
    existing weighting engine can consume it without changing its public API.
    Positive values favor higher USD/MXN; negative values favor lower USD/MXN.
    """
    if not momentum:
        return None
    windows = momentum.get("windows") or {}
    signed_score = 0.0
    used_weight = 0.0
    diagnostics = []

    for label, weight in _WINDOW_WEIGHTS.items():
        row = windows.get(label) or {}
        change = _f(row.get("change"))
        if change is None:
            continue
        norm = _WINDOW_NORMS[label]
        normalized = max(-1.0, min(1.0, change / norm))
        signed_score += normalized * weight
        used_weight += weight
        diagnostics.append({
            "window": label,
            "change": round(change, 4),
            "normalized": round(normalized, 3),
            "weight": weight,
        })

    if used_weight <= 0:
        # Backward compatibility for older data with only consecutive snapshots.
        change = _f(momentum.get("change"))
        if change is None:
            return None
        return dict(momentum)

    score = signed_score / used_weight
    acceleration = _f(momentum.get("acceleration_per_minute"))
    if acceleration is not None:
        # Small acceleration nudge; cap it so one noisy 5m print cannot dominate.
        accel_nudge = max(-0.15, min(0.15, acceleration / 0.0015))
        score = max(-1.0, min(1.0, score + accel_nudge))

    latest_to = _f(momentum.get("to"))
    earliest = None
    for label in ("60m", "30m", "15m", "5m"):
        row = windows.get(label) or {}
        if row.get("from") is not None:
            earliest = _f(row.get("from"))
            break

    return {
        "change": round(score * 0.05, 4),
        "from": earliest if earliest is not None else momentum.get("from"),
        "to": latest_to if latest_to is not None else momentum.get("to"),
        "tactical_score": round(score, 3),
        "windows": windows,
        "window_diagnostics": diagnostics,
        "acceleration_per_minute": acceleration,
        "source": momentum.get("source") or "stored_market_snapshots",
    }
