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
import math
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
    """Representative USD/MXN move (1d, falling back to 4h/1h/5d/30d)."""
    w = match.get("windows") or {}
    for key in ("1d", "4h", "1h", "5d", "30d", "3d"):
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
    reversals = 0
    reversal_known = 0
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
        behavior = m.get("reversal_behavior")
        if behavior and behavior != "unknown":
            reversal_known += 1
            if behavior == "reversal":
                reversals += 1

    if not rep_moves:
        return {
            "sample_size": 0,
            "wins": 0,
            "average_move": None,
            "median_move": None,
            "best_move": None,
            "worst_move": None,
            "win_rate": None,
            "expected_range": None,
            "expected_duration": "n/a",
            "expected_duration_hours": None,
            "average_holding_hours": None,
            "typical_MFE": None,
            "typical_MAE": None,
            "average_MFE": None,
            "average_MAE": None,
            "max_drawdown": None,
            "reversal_probability": None,
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

    def _mean(vals: list[float]) -> float | None:
        return round(sum(vals) / len(vals), 4) if vals else None

    return {
        "sample_size": counted,
        "wins": wins,
        "average_move": avg,
        "median_move": _median(rep_moves),
        "best_move": round(max(rep_moves), 4),
        "worst_move": round(min(rep_moves), 4),
        "win_rate": round(wins / counted * 100.0, 1) if counted else None,
        "expected_range": expected_range,
        "expected_duration": _duration_label(mean_peak),
        "expected_duration_hours": mean_peak,
        "average_holding_hours": mean_peak,
        "typical_MFE": _median(mfes),
        "typical_MAE": _median(maes),
        "average_MFE": _mean(mfes),
        "average_MAE": _mean(maes),
        "max_drawdown": round(max(maes), 4) if maes else None,
        "reversal_probability": (
            round(reversals / reversal_known * 100.0, 1) if reversal_known else None
        ),
    }


def wilson_interval(hits: int, n: int, z: float = 1.96) -> list[float] | None:
    """95% Wilson score confidence interval for a proportion, in percent.

    Wilson is used instead of the naive normal interval because it stays inside
    [0, 100] and behaves well for small samples — important here, where the
    evidence base can be a few dozen analogs rather than thousands.
    """
    if n <= 0:
        return None
    p = hits / n
    denom = 1.0 + z * z / n
    centre = p + z * z / (2 * n)
    margin = z * math.sqrt((p * (1.0 - p) + z * z / (4 * n)) / n)
    lo = max(0.0, (centre - margin) / denom)
    hi = min(1.0, (centre + margin) / denom)
    return [round(lo * 100.0, 1), round(hi * 100.0, 1)]


def _evidence(hits: int, n: int, basis: str) -> dict:
    """Package a probability as evidence: value + sample size + CI + basis."""
    value = round(hits / n * 100.0, 1) if n else None
    return {
        "value": value,
        "sample_size": n,
        "hits": hits,
        "confidence_interval": wilson_interval(hits, n),
        "basis": basis,
    }


def probability_forecast(
    matches: list[dict],
    current_price: float | None,
    direction: str,
    targets: dict,
) -> dict:
    """Evidence-based probabilities from the similar-event sample.

    ``targets`` may contain ``target_1``, ``target_2``, ``stretch``, ``stop``
    (absolute USD/MXN prices). Each probability is the fraction of similar
    events whose favorable / adverse excursion (in our trade direction) reached
    the required move, and now carries its own sample size, 95% Wilson
    confidence interval, and a plain-language historical basis.

    Backward compatible: the simple ``levels`` floats are preserved; the new
    per-probability detail lives under ``evidence``.
    """
    our_dir = _DIR.get(direction, 1.0)
    favs: list[float] = []   # max favorable move % in our direction
    advs: list[float] = []   # max adverse move % in our direction
    win_windows: dict[str, list[float]] = {"1d": [], "3d": [], "5d": []}
    for m in matches:
        rets = _present_windows(m)
        if not rets:
            continue
        signed = [r * our_dir for r in rets]
        favs.append(max(signed))
        advs.append(max(-min(signed), 0.0))
        w = m.get("windows") or {}
        for key in win_windows:
            if w.get(key) is not None:
                win_windows[key].append(w[key] * our_dir)

    n = len(favs)
    out: dict = {"sample_size": n, "levels": {}, "evidence": {}}
    if n == 0 or not current_price:
        out["method"] = (
            "Evidence-based: fraction of the matched historical analogs whose "
            "USD/MXN excursion reached each level (insufficient sample here)."
        )
        return out

    def level_hits(level_price: float | None, adverse: bool = False) -> tuple[int, int, float] | None:
        if level_price is None:
            return None
        move_pct = (level_price / current_price - 1.0) * 100.0 * our_dir
        required = abs(move_pct)
        series = advs if adverse else favs
        hits = sum(1 for v in series if v >= required)
        return hits, len(series), round(required, 4)

    def reach_prob(level_price: float | None, adverse: bool = False) -> float | None:
        res = level_hits(level_price, adverse=adverse)
        if res is None:
            return None
        hits, total, _ = res
        return round(hits / total * 100.0, 1) if total else None

    def reach_evidence(level_price: float | None, label: str, adverse: bool = False) -> dict | None:
        res = level_hits(level_price, adverse=adverse)
        if res is None:
            return None
        hits, total, required = res
        verb = "moved against the trade by" if adverse else "reached a favorable move of"
        basis = f"{hits} of {total} analogs {verb} ≥{required}% ({label})."
        return _evidence(hits, total, basis)

    def positive_evidence(window_key: str, label: str) -> dict | None:
        series = win_windows.get(window_key) or []
        if not series:
            return None
        hits = sum(1 for v in series if v > 0)
        basis = f"{hits} of {len(series)} analogs finished in profit {label} ({window_key} window)."
        return _evidence(hits, len(series), basis)

    out["levels"] = {
        "probability_reaches_target_1": reach_prob(targets.get("target_1")),
        "probability_reaches_target_2": reach_prob(targets.get("target_2")),
        "probability_reaches_stretch": reach_prob(targets.get("stretch")),
        "probability_hits_stop": reach_prob(targets.get("stop"), adverse=True),
        "probability_finishes_positive_today": (
            round(sum(1 for v in (win_windows.get("1d") or []) if v > 0)
                  / len(win_windows["1d"]) * 100.0, 1)
            if win_windows.get("1d") else None
        ),
        "probability_finishes_positive_tomorrow": (
            round(sum(1 for v in (win_windows.get("3d") or []) if v > 0)
                  / len(win_windows["3d"]) * 100.0, 1)
            if win_windows.get("3d") else None
        ),
        "probability_finishes_positive_within_5d": (
            round(sum(1 for v in (win_windows.get("5d") or []) if v > 0)
                  / len(win_windows["5d"]) * 100.0, 1)
            if win_windows.get("5d") else None
        ),
    }
    out["evidence"] = {
        "reaches_target": reach_evidence(targets.get("target_1"), "target"),
        "reaches_stretch": reach_evidence(targets.get("stretch"), "stretch target"),
        "reaches_stop": reach_evidence(targets.get("stop"), "protective stop", adverse=True),
        "finishes_positive_today": positive_evidence("1d", "by end of day"),
        "finishes_positive_tomorrow": positive_evidence("3d", "next day"),
        "finishes_positive_within_5d": positive_evidence("5d", "within five days"),
    }
    out["targets"] = targets
    out["method"] = (
        "Evidence-based: each probability is the share of the matched historical "
        "analogs whose USD/MXN excursion reached the level, with a 95% Wilson "
        "confidence interval reflecting the sample size."
    )
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


# Human-readable labels for each confidence component (the six conceptual
# inputs requested in the evidence engine map onto these canonical keys; the
# combined news + calendar richness is carried by ``data_quality``).
_CONFIDENCE_LABELS: dict[str, str] = {
    "signal": "Current signals",
    "historical": "Historical evidence",
    "regime": "Market-regime confidence",
    "volatility": "Volatility (calmer = higher quality)",
    "data_quality": "Data completeness (news + calendar)",
}


def blend_confidence(components: dict, settings: Settings | None = None) -> dict:
    """Blend 0..100 confidence components with configurable weights.

    Missing/None components are skipped and the remaining weights renormalized,
    so absent historical data never *lowers* confidence — it just isn't counted.
    Returns ``{value, components, weights_used, explanation, formula}`` where the
    explanation enumerates exactly how the score was computed.
    """
    weights = get_confidence_weights(settings)
    acc = 0.0
    wsum = 0.0
    used: dict[str, float] = {}
    explanation: list[str] = []
    terms: list[str] = []
    for key, w in weights.items():
        val = components.get(key)
        if val is None or w <= 0:
            continue
        v = max(0.0, min(100.0, float(val)))
        acc += w * v
        wsum += w
        used[key] = w
        label = _CONFIDENCE_LABELS.get(key, key)
        contribution = round(w * v, 2)
        explanation.append(
            f"{label}: {round(v, 1)}/100 × weight {w} = {contribution}"
        )
        terms.append(f"{w}×{round(v, 1)}")

    if wsum > 0:
        raw = acc / wsum
        value = round(min(95.0, raw), 1)
        formula = (
            f"weighted blend = ({' + '.join(terms)}) / {round(wsum, 3)} "
            f"= {round(raw, 1)} → capped at 95 = {value}"
        )
    else:
        value = None
        formula = "no usable components"

    return {
        "value": value,
        "components": components,
        "weights_used": used,
        "explanation": explanation,
        "formula": formula,
    }


def setup_percentile(reference_moves: list[float], value: float | None) -> float | None:
    """Percentile rank (0..100) of ``value`` within ``reference_moves``.

    Used to answer *"how strong is today's setup vs history?"* — e.g. an
    expected directional move that beats 91% of past directional moves ranks in
    the 91st percentile.
    """
    if value is None or not reference_moves:
        return None
    below = sum(1 for x in reference_moves if x <= value)
    return round(below / len(reference_moves) * 100.0, 1)


def evidence_narrative(
    stats: dict,
    direction: str,
    percentile: float | None = None,
    *,
    database_size: int | None = None,
    comparable_count: int | None = None,
    since_year: int | None = None,
) -> str | None:
    """Render the headline evidence sentence(s) for the strategist brief."""
    n = stats.get("sample_size") or 0
    if n == 0:
        return None
    wins = stats.get("wins")
    win_rate = stats.get("win_rate")
    avg = stats.get("average_move")
    median = stats.get("median_move")
    hold = stats.get("average_holding_hours")
    mae = stats.get("max_drawdown")

    if direction == "BUY_USD":
        subject, moved = "USD", "strengthened"
        lean_word = "bullish-USD"
    elif direction == "SELL_USD":
        subject, moved = "the peso", "strengthened"
        lean_word = "bearish-USD"
    else:
        subject, moved = "the trade lean", "played out"
        lean_word = "comparable"

    parts: list[str] = []
    if database_size and since_year:
        parts.append(
            f"Searched {database_size} daily market environments since {since_year}; "
            f"{comparable_count or n} comparable analog(s) inform this read."
        )
    else:
        parts.append(f"Historically, conditions similar to today occurred {n} times.")
    if wins is not None:
        parts.append(f"{subject.capitalize()} {moved} in {wins} of those cases"
                     + (f" ({win_rate}%)." if win_rate is not None else "."))
    if avg is not None:
        parts.append(f"Average move {avg:+.2f}%"
                     + (f", median {median:+.2f}%." if median is not None else "."))
    if hold is not None:
        parts.append(f"Average holding time {round(hold)} hours.")
    if mae is not None:
        parts.append(f"Largest adverse excursion -{abs(mae):.2f}%.")
    if percentile is not None:
        parts.append(
            f"Current setup ranks in the {round(percentile)}th percentile of "
            f"{lean_word} historical setups."
        )
    return " ".join(parts)
