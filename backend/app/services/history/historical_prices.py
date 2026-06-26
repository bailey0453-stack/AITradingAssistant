"""Reaction-window math for historical events.

Given a price *path* sampled at offsets after an event (hours -> USD/MXN price)
and the baseline price at the event time, compute the standard reaction
statistics used throughout Phase 4:

  - windowed returns: 15m, 1h, 4h, 1d, 3d, 5d (percent moves of USD/MXN)
  - max favorable / adverse excursion (relative to the net reaction direction)
  - time to peak (hours)
  - reversal behavior (continuation | reversal | fade)
  - data completeness (fraction of windows with data)

These are intentionally pure functions so they can be driven by mock/sample
paths today and real intraday bars later — the importer is the only thing that
changes.
"""

from __future__ import annotations

# Window label -> hours after the event.
WINDOWS: dict[str, float] = {
    "15m": 0.25,
    "1h": 1.0,
    "4h": 4.0,
    "1d": 24.0,
    "3d": 72.0,
    "5d": 120.0,
}


def _price_at(path: list[tuple[float, float]], hours: float) -> float | None:
    """Price at the latest sampled offset <= ``hours`` (step interpolation)."""
    chosen = None
    for h, p in path:
        if h <= hours + 1e-9:
            chosen = p
        else:
            break
    return chosen


def compute_reaction_windows(
    path: list[tuple[float, float]],
    baseline: float,
) -> dict:
    """Compute reaction statistics from a sorted (hours, price) path.

    ``baseline`` is the USD/MXN price at the event time (offset 0). Returns a
    dict keyed by ``ret_15m``..``ret_5d`` plus excursion / timing / quality.
    """
    if not baseline:
        return {"data_completeness": 0.0}

    path = sorted(path, key=lambda x: x[0])

    def pct(p: float | None) -> float | None:
        if p is None:
            return None
        return round((p / baseline - 1.0) * 100.0, 4)

    rets: dict[str, float | None] = {}
    have = 0
    for label, hrs in WINDOWS.items():
        r = pct(_price_at(path, hrs))
        rets[f"ret_{label}"] = r
        if r is not None:
            have += 1
    completeness = round(have / len(WINDOWS), 3)

    # Net direction of the reaction (use the longest available window).
    net = None
    for label in ("5d", "3d", "1d", "4h", "1h", "15m"):
        if rets[f"ret_{label}"] is not None:
            net = rets[f"ret_{label}"]
            break
    direction = 1.0 if (net or 0.0) >= 0 else -1.0

    # Excursions in the direction of the reaction (favorable) and against (adverse).
    signed_series = [
        (h, (p / baseline - 1.0) * 100.0 * direction) for h, p in path
    ]
    mfe = mae = 0.0
    time_to_peak = None
    if signed_series:
        best = max(signed_series, key=lambda x: x[1])
        worst = min(signed_series, key=lambda x: x[1])
        mfe = round(max(0.0, best[1]), 4)
        mae = round(max(0.0, -worst[1]), 4)
        time_to_peak = round(best[0], 3)

    # Reversal behavior: early (1h) vs late (5d/last) sign + magnitude.
    early = rets.get("ret_1h")
    late = net
    if early is None or late is None:
        reversal = "unknown"
    elif (early >= 0) != (late >= 0):
        reversal = "reversal"
    elif abs(late) >= abs(early):
        reversal = "continuation"
    else:
        reversal = "fade"

    return {
        **rets,
        "max_favorable_excursion": mfe,
        "max_adverse_excursion": mae,
        "time_to_peak_hours": time_to_peak,
        "reversal_behavior": reversal,
        "data_completeness": completeness,
    }
