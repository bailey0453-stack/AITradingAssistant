"""Aggregate statistics, probability forecast, and confidence blending.

Turns a set of similar historical reactions into:
  - aggregate stats (average/median move, win rate, expected range/duration,
    typical MFE/MAE), and
  - a probability forecast for hitting specific target/stop levels.

Also hosts the configurable ``blend_confidence`` used by the analysis engine to
combine live signal strength with historical similarity, regime confidence,
volatility, and data quality. Weights are configurable (settings override).
"""

from __future__ import annotations

import logging
import statistics

from app.config import Settings, get_settings

logger = logging.getLogger(__name__)

# Configurable weights for blended confidence (settings.confidence_weights).
DEFAULT_CONFIDENCE_WEIGHTS: dict[str, float] = {
    "signal": 0.40,        # current weighted signal confidence
    "historical": 0.25,    # historical similarity quality
    "regime": 0.15,        # market regime confidence
    "volatility": 0.10,    # calmer tape -> higher quality
    "data_quality": 0.10,  # richness of news/calendar inputs
}

_DIR = {"BUY_USD": 1.0, "SELL_USD": -1.0, "NO_TRADE": 1.0}


def _present_windows(match: dict) -> list[float]:
    return [v for v in (match.get("windows") or {}).values() if v is not None]


def _rep_move(match: dict) -> float | None:
    """Representative USD/MXN move (1d, falling back to 4h/1h)."""
    w = match.get("windows") or {}
    for key in ("1d", "4h", "1h", "3d", "5d"):
        if w.get(key) is not None:
            return w[key]
    return None


def _median(values: list[float]) -> float | None:
    return round(statistics.median(values), 4) if values else None


def _percentile(values: list[float], pct: float) -> float | None:
    if not values:
        return None
    s = sorted(values)
    if len(s) == 1:
        return round(s[0], 4)
    k = (len(s) - 1) * pct
    lo = int(k)
    hi = min(lo + 1, len(s) - 1)
    frac = k - lo
    return round(s[lo] + (s[hi] - s[lo]) * frac, 4)


def _duration_label(hours: float | None) -> str:
    if hours is None:
        return "n/a"
    if hours <= 6:
        return "intraday (<6h)"
    if hours <= 30:
        return "~1 day"
    if hours <= 80:
        return "2-3 days"
    return "3-5 days"


def aggregate_statistics(matches: list[dict], direction: str = "NO_TRADE",
                         current_price: float | None = None) -> dict:
    """Aggregate similar-event reactions into summary statistics."""
    our_dir = _DIR.get(direction, 1.0)
    rep_moves: list[float] = []
    weights: list[float] = []
    mfes: list[float] = []
    maes: list[float] = []
    peaks: list[float] = []
    wins = 0
    counted = 0

    for m in matches:
        rep = _rep_move(m)
        if rep is None:
            continue
        rep_moves.append(rep)
        weights.append(float(m.get("similarity_score") or 0.0))
        counted += 1
        if (rep >= 0) == (our_dir >= 0):
            wins += 1
        if m.get("max_favorable_excursion") is not None:
            mfes.append(m["max_favorable_excursion"])
        if m.get("max_adverse_excursion") is not None:
            maes.append(m["max_adverse_excursion"])
        if m.get("time_to_peak_hours") is not None:
            peaks.append(m["time_to_peak_hours"])

    if not rep_moves:
        return {
            "sample_size": 0,
            "average_move": None,
            "median_move": None,
            "win_rate": None,
            "expected_range": None,
            "expected_duration": "n/a",
            "typical_MFE": None,
            "typical_MAE": None,
        }

    wsum = sum(weights)
    if wsum > 0:
        avg = round(sum(r * w for r, w in zip(rep_moves, weights)) / wsum, 4)
    else:
        avg = round(sum(rep_moves) / len(rep_moves), 4)

    p25 = _percentile(rep_moves, 0.25)
    p75 = _percentile(rep_moves, 0.75)
    mean_peak = round(sum(peaks) / len(peaks), 2) if peaks else None

    expected_range: dict | None = None
    if p25 is not None and p75 is not None:
        expected_range = {"low_pct": p25, "high_pct": p75}
        if current_price:
            expected_range["low_price"] = round(current_price * (1 + p25 / 100.0), 4)
            expected_range["high_price"] = round(current_price * (1 + p75 / 100.0), 4)

    return {
        "sample_size": counted,
        "average_move": avg,
        "median_move": _median(rep_moves),
        "win_rate": round(wins / counted * 100.0, 1) if counted else None,
        "expected_range": expected_range,
        "expected_duration": _duration_label(mean_peak),
        "expected_duration_hours": mean_peak,
        "typical_MFE": _median(mfes),
        "typical_MAE": _median(maes),
    }


def probability_forecast(
    matches: list[dict],
    current_price: float | None,
    direction: str,
    targets: dict,
) -> dict:
    """Estimate probabilities of reaching target/stretch levels and hitting stop.

    ``targets`` may contain ``target_1``, ``target_2``, ``stretch``, ``stop``
    (absolute USD/MXN prices). Probabilities are the fraction of similar events
    whose favorable / adverse excursion (in our trade direction) reached the
    required move.
    """
    our_dir = _DIR.get(direction, 1.0)
    favs: list[float] = []   # max favorable move % in our direction
    advs: list[float] = []   # max adverse move % in our direction
    for m in matches:
        rets = _present_windows(m)
        if not rets:
            continue
        signed = [r * our_dir for r in rets]
        favs.append(max(signed))
        advs.append(max(-min(signed), 0.0))

    n = len(favs)
    out: dict = {"sample_size": n, "levels": {}}
    if n == 0 or not current_price:
        return out

    def reach_prob(level_price: float | None, adverse: bool = False) -> float | None:
        if level_price is None:
            return None
        # Required move magnitude in our direction (favorable) or against (stop).
        move_pct = (level_price / current_price - 1.0) * 100.0 * our_dir
        required = abs(move_pct)
        series = advs if adverse else favs
        hits = sum(1 for v in series if v >= required)
        return round(hits / n * 100.0, 1)

    out["levels"] = {
        "probability_reaches_target_1": reach_prob(targets.get("target_1")),
        "probability_reaches_target_2": reach_prob(targets.get("target_2")),
        "probability_reaches_stretch": reach_prob(targets.get("stretch")),
        "probability_hits_stop": reach_prob(targets.get("stop"), adverse=True),
    }
    out["targets"] = targets
    return out


def get_confidence_weights(settings: Settings | None = None) -> dict[str, float]:
    settings = settings or get_settings()
    weights = dict(DEFAULT_CONFIDENCE_WEIGHTS)
    override = getattr(settings, "confidence_weights", None)
    if isinstance(override, dict):
        for k, v in override.items():
            if k not in DEFAULT_CONFIDENCE_WEIGHTS:
                logger.warning("Ignoring unknown confidence weight key: %r", k)
                continue
            try:
                weights[k] = float(v)
            except (TypeError, ValueError):
                logger.warning("Ignoring non-numeric confidence weight %r: %r", k, v)
    return weights


def blend_confidence(components: dict, settings: Settings | None = None) -> dict:
    """Blend 0..100 confidence components with configurable weights.

    Missing/None components are skipped and the remaining weights renormalized,
    so absent historical data never *lowers* confidence — it just isn't counted.
    Returns ``{value, components, weights_used}``.
    """
    weights = get_confidence_weights(settings)
    acc = 0.0
    wsum = 0.0
    used: dict[str, float] = {}
    for key, w in weights.items():
        val = components.get(key)
        if val is None or w <= 0:
            continue
        v = max(0.0, min(100.0, float(val)))
        acc += w * v
        wsum += w
        used[key] = w
    value = round(min(95.0, acc / wsum), 1) if wsum > 0 else None
    return {"value": value, "components": components, "weights_used": used}
