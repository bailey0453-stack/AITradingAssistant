"""Phase 5.4 Evidence & Provenance Engine.

Annotates the major analysis outputs with *where each number came from* and
*how trustworthy it is*, so the dashboard never makes an AI estimate look like
real market data.

Evidence levels (high -> low trust):
    5  measured    — computed from stored recommendation outcomes
    4  historical  — computed from the historical market database
    3  live        — current live provider data
    2  cached      — previously verified live data, served from cache
    1  estimated   — reasoning-engine output (not yet confirmed by outcomes)
    0  sample      — mock / demo data

This module only describes provenance — it never changes any calculation. As
recommendation outcomes accumulate, estimated trade-plan metrics are *labeled*
measured where appropriate (the value itself is untouched).
"""

from __future__ import annotations

from collections import defaultdict
from typing import Optional

# source -> evidence level
LEVELS = {
    "measured": 5,
    "historical": 4,
    "live": 3,
    "cached": 2,
    "estimated": 1,
    "sample": 0,
}
BADGES = {
    "measured": "MEASURED",
    "historical": "HISTORICAL",
    "live": "LIVE",
    "cached": "CACHED",
    "estimated": "ESTIMATED",
    "sample": "SAMPLE",
}
TOOLTIPS = {
    "measured": "Computed from stored recommendation outcomes.",
    "historical": "Computed from the historical market database.",
    "live": "Current live provider data.",
    "cached": "Previously verified live data, served from cache.",
    "estimated": "Reasoning-engine estimate — not yet confirmed by outcomes.",
    "sample": "Mock / demo sample data.",
}
# Ordered high-trust -> low-trust for display.
SOURCE_ORDER = ["measured", "historical", "live", "cached", "estimated", "sample"]


def tag(value, source: str, *, provider: Optional[str] = None,
        timestamp: Optional[str] = None, confidence: Optional[float] = None,
        label: Optional[str] = None, note: Optional[str] = None) -> dict:
    """Wrap a value with its provenance metadata."""
    source = source if source in LEVELS else "estimated"
    out = {
        "value": value,
        "source": source,
        "evidence_level": LEVELS[source],
        "badge": BADGES[source],
        "explanation": TOOLTIPS[source],
    }
    if provider is not None:
        out["provider"] = provider
    if timestamp is not None:
        out["timestamp"] = timestamp
    if confidence is not None:
        out["confidence"] = confidence
    if label is not None:
        out["label"] = label
    if note is not None:
        out["note"] = note
    return out


def _market_field_source(field_src: Optional[str], cached: bool) -> str:
    """Map a market field's raw source to a provenance source."""
    fs = (field_src or "").lower()
    if fs == "live":
        return "cached" if cached else "live"
    if fs == "cached":
        return "cached"
    if fs in ("mock", "fallback", "sample", ""):
        return "sample"
    return "estimated"


# historical data-source label -> (provenance source, human DB label)
_HIST_DB = {
    "sample": ("sample", "Sample Historical Database"),
    "backfilled": ("historical", "Historical Database"),
    "live": ("historical", "Historical Database"),
    "imported": ("historical", "Historical Database"),
    "measured": ("measured", "Measured Recommendation History"),
}


def _hist_source(payload: dict) -> tuple[str, str]:
    hist = (payload.get("data_sources") or {}).get("historical", "sample")
    return _HIST_DB.get(str(hist).lower(), ("sample", "Sample Historical Database"))


def _first_probability(payload: dict):
    levels = (payload.get("probabilities") or {}).get("levels") or {}
    return levels.get("probability_reaches_target_1")


def build(payload: dict, market_meta: dict, *,
          measured_accuracy: Optional[float] = None,
          measured_available: bool = False,
          similar_measured: bool = False) -> dict:
    """Build a provenance map ``{field: {value, source, evidence_level, ...}}``.

    - ``measured_available`` / ``measured_accuracy``: stored recommendation
      outcomes exist (and the headline accuracy if so).
    - ``similar_measured``: there is enough *similar* recommendation history to
      back the trade plan — when true, estimated trade-plan metrics are labeled
      ``measured`` (values are never changed).
    """
    prov: dict = {}
    market = payload.get("market") or {}
    fsrc = market.get("sources") or {}
    cached = bool(market_meta.get("cached"))
    provider = market_meta.get("provider") or market.get("provider")
    ts = market_meta.get("fetched_at") or market.get("created_at")

    # --- live / cached / sample market data ---
    spot_source = _market_field_source(fsrc.get("usdmxn", market.get("source")), cached)
    prov["spot_rate"] = tag(market.get("usdmxn"), spot_source,
                            provider=provider, timestamp=ts)
    for field in ("dxy", "us2y", "us10y", "oil", "gold", "sp_futures", "vix"):
        if market.get(field) is None:
            continue
        prov[field] = tag(market.get(field),
                          _market_field_source(fsrc.get(field, "mock"), cached),
                          provider=provider, timestamp=ts)

    # --- estimated trade plan (auto-labels measured once history supports it) ---
    plan_source = "measured" if similar_measured else "estimated"
    plan_note = (
        "Backed by measured similar-recommendation history."
        if similar_measured else
        "Reasoning-engine estimate until enough similar outcomes accumulate."
    )
    prov["entry"] = tag(payload.get("entry"), spot_source, provider=provider, timestamp=ts)
    for field in ("target", "stretch_target", "stop", "expected_move"):
        prov[field] = tag(payload.get(field), plan_source, note=plan_note)
    prov["probabilities"] = tag(_first_probability(payload), plan_source, note=plan_note)

    # --- confidence + decision quality: reasoning-engine estimates ---
    prov["confidence"] = tag(payload.get("confidence"), "estimated")
    dq = payload.get("decision_quality") or {}
    prov["trade_quality_score"] = tag(dq.get("trade_quality_score"), "estimated")
    prov["expected_value"] = tag(
        (dq.get("expected_value") or {}).get("expected_value_usd"), plan_source, note=plan_note
    )

    # --- historical similarity / win rate: historical-database evidence ---
    hist_source, db_label = _hist_source(payload)
    hist = payload.get("historical") or {}
    sim = hist.get("best_similarity") if isinstance(hist, dict) else None
    prov["historical_similarity"] = tag(
        round(sim * 100, 1) if isinstance(sim, (int, float)) else None,
        hist_source, label=db_label,
    )
    hist_wr = ((hist.get("statistics") or {}) if isinstance(hist, dict) else {}).get("win_rate")
    prov["historical_win_rate"] = tag(hist_wr, hist_source, label=db_label)

    # --- recommendation accuracy + similar track record: measured outcomes ---
    prov["recommendation_accuracy"] = tag(
        measured_accuracy if measured_available else None,
        "measured" if measured_available else "estimated",
        label="Measured Recommendation History",
        note=(None if measured_available else
              "Not enough scored recommendation outcomes yet — measured accuracy unavailable."),
    )
    similar = dq.get("similar_track_record") or {}
    prov["similar_track_record"] = tag(
        similar.get("similar_win_rate") if similar_measured else None,
        "measured" if similar_measured else "estimated",
        label="Measured Recommendation History",
        note=(None if similar_measured else similar.get("note")),
    )

    return prov


def overview(prov: dict) -> dict:
    """Evidence-summary card: how much of today's analysis is evidence vs inference."""
    counts = {s: 0 for s in LEVELS}
    fields_by_source: dict[str, list[str]] = defaultdict(list)
    for field, meta in prov.items():
        src = meta.get("source", "estimated")
        counts[src] = counts.get(src, 0) + 1
        fields_by_source[src].append(field)

    total = sum(counts.values())
    by_source = {
        src: {
            "count": counts[src],
            "evidence_level": LEVELS[src],
            "badge": BADGES[src],
            "explanation": TOOLTIPS[src],
            "fields": sorted(fields_by_source.get(src, [])),
        }
        for src in SOURCE_ORDER
    }

    def share(*sources) -> float:
        return round(100 * sum(counts[s] for s in sources) / total, 1) if total else 0.0

    return {
        "total_metrics": total,
        "counts": counts,
        "order": SOURCE_ORDER,
        "by_source": by_source,
        "live_metrics": counts["live"],
        "cached_metrics": counts["cached"],
        "measured_metrics": counts["measured"],
        "historical_metrics": counts["historical"],
        "estimated_metrics": counts["estimated"],
        "sample_metrics": counts["sample"],
        "measured_share_pct": share("measured"),
        "evidence_backed_share_pct": share("measured", "historical", "live", "cached"),
        "estimated_share_pct": share("estimated"),
        "sample_share_pct": share("sample"),
    }
