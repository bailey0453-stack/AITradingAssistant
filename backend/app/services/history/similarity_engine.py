"""Similarity engine: "find events like this one".

Builds a feature vector from the *current* context and scores it against every
stored historical reaction (which carries its own pre-event context). Scoring is
a weighted blend of per-feature similarities; weights are configurable so the
model can be tuned without touching the engine.

Per-feature similarity:
  - categorical (regime, event_type): 1.0 if equal else 0.0
  - numeric (dxy, yields, oil, gold, vix, sp, momentum): Gaussian
    ``exp(-(diff/scale)^2)`` with a per-feature scale (typical variation)
  - news_tags: Jaccard overlap of tag sets

The final score is the weighted average over features that are present on both
sides (missing features are skipped and the weights renormalized).
"""

from __future__ import annotations

import logging
import math

from sqlalchemy.orm import Session

from app.config import Settings, get_settings
from app.models import SimilarityMatch
from app.services.history.historical_events import ensure_history_seeded, load_reactions
from app.services.signal_weights import event_signal_key, news_category

logger = logging.getLogger(__name__)

# Default feature weights (tunable via settings.similarity_weights).
DEFAULT_SIMILARITY_WEIGHTS: dict[str, float] = {
    "regime": 0.18,
    "event_type": 0.18,
    "vix": 0.12,
    "dxy": 0.12,
    "us2y": 0.06,
    "us10y": 0.06,
    "oil": 0.08,
    "momentum": 0.10,
    "news_tags": 0.06,
    "sp_futures": 0.04,
    "gold": 0.04,
}

# Per-feature Gaussian scale = "how far apart counts as clearly different".
_SCALES: dict[str, float] = {
    "vix": 6.0,
    "dxy": 3.0,
    "us2y": 0.7,
    "us10y": 0.6,
    "oil": 8.0,
    "momentum": 0.06,
    "sp_futures": 250.0,
    "gold": 120.0,
}

_NUMERIC = ("vix", "dxy", "us2y", "us10y", "oil", "momentum", "sp_futures", "gold")


def get_similarity_weights(settings: Settings | None = None) -> dict[str, float]:
    settings = settings or get_settings()
    weights = dict(DEFAULT_SIMILARITY_WEIGHTS)
    override = getattr(settings, "similarity_weights", None)
    if isinstance(override, dict):
        for k, v in override.items():
            if k not in DEFAULT_SIMILARITY_WEIGHTS:
                logger.warning("Ignoring unknown similarity weight key: %r", k)
                continue
            try:
                weights[k] = float(v)
            except (TypeError, ValueError):
                logger.warning("Ignoring non-numeric similarity weight %r: %r", k, v)
    return weights


def _dominant_event_type(context: dict) -> str | None:
    """Pick the most relevant event type from recent/upcoming calendar items."""
    events = (context.get("released_last_24h") or []) + (context.get("upcoming_events") or [])
    for ev in events:
        if ev.get("importance") == "high":
            key = event_signal_key(ev.get("event", ""))
            if key:
                return key
    for ev in events:
        key = event_signal_key(ev.get("event", ""))
        if key:
            return key
    return None


def _news_tags(context: dict) -> list[str]:
    tags: set[str] = set()
    for item in context.get("recent_news") or []:
        for t in (item.get("tags") or []):
            tags.add(str(t).lower())
        tags.add(news_category(item))
    return sorted(tags)


def build_feature_vector(context: dict, regime: dict | None = None) -> dict:
    """Assemble the current-context feature vector used for matching."""
    market = context.get("market") or {}
    momentum = context.get("momentum") or {}
    regime = regime or context.get("market_regime") or {}
    return {
        "regime": (regime or {}).get("primary"),
        "event_type": _dominant_event_type(context),
        "dxy": market.get("dxy"),
        "us2y": market.get("us2y"),
        "us10y": market.get("us10y"),
        "oil": market.get("oil"),
        "gold": market.get("gold"),
        "vix": market.get("vix"),
        "sp_futures": market.get("sp_futures"),
        "momentum": momentum.get("change"),
        "news_tags": _news_tags(context),
    }


def _jaccard(a: list | None, b: list | None) -> float | None:
    sa, sb = set(a or []), set(b or [])
    if not sa and not sb:
        return None
    union = sa | sb
    if not union:
        return None
    return len(sa & sb) / len(union)


def score_reaction(query: dict, reaction: dict, weights: dict[str, float]) -> float:
    """Weighted similarity (0..1) between the query vector and one reaction."""
    ctx = reaction.get("context") or {}
    total_w = 0.0
    acc = 0.0

    # Categorical: regime + event_type.
    for key, qval, rval in (
        ("regime", query.get("regime"), ctx.get("regime")),
        ("event_type", query.get("event_type"), reaction.get("event_type")),
    ):
        if qval is None or rval is None:
            continue
        w = weights.get(key, 0.0)
        if w <= 0:
            continue
        acc += w * (1.0 if str(qval) == str(rval) else 0.0)
        total_w += w

    # Numeric: Gaussian similarity.
    for key in _NUMERIC:
        qv, rv = query.get(key), ctx.get(key)
        if qv is None or rv is None:
            continue
        w = weights.get(key, 0.0)
        if w <= 0:
            continue
        scale = _SCALES.get(key, 1.0) or 1.0
        sim = math.exp(-((float(qv) - float(rv)) / scale) ** 2)
        acc += w * sim
        total_w += w

    # News tags: Jaccard.
    j = _jaccard(query.get("news_tags"), ctx.get("news_tags"))
    if j is not None:
        w = weights.get("news_tags", 0.0)
        if w > 0:
            acc += w * j
            total_w += w

    if total_w <= 0:
        return 0.0
    return round(acc / total_w, 4)


def find_similar(
    db: Session,
    context: dict,
    regime: dict | None = None,
    top_n: int = 5,
    persist: bool = False,
    analysis_snapshot_id: int | None = None,
    settings: Settings | None = None,
) -> dict:
    """Rank historical reactions by similarity to the current context."""
    settings = settings or get_settings()
    weights = get_similarity_weights(settings)
    query = build_feature_vector(context, regime=regime)

    ensure_history_seeded(db)
    reactions = load_reactions(db)

    scored = []
    for r in reactions:
        s = score_reaction(query, r, weights)
        item = dict(r)
        item["similarity_score"] = s
        scored.append(item)
    scored.sort(key=lambda x: x["similarity_score"], reverse=True)
    top = scored[:top_n]

    if persist and top:
        persist_matches(db, query, top, analysis_snapshot_id)

    best = top[0]["similarity_score"] if top else 0.0
    return {
        "query_vector": query,
        "weights": weights,
        "considered": len(reactions),
        "top_matches": top,
        "best_similarity": best,
    }


def persist_matches(
    db: Session,
    query_vector: dict,
    matches: list[dict],
    analysis_snapshot_id: int | None = None,
) -> int:
    """Persist ranked matches to ``similarity_matches`` (best-effort)."""
    try:
        for rank, m in enumerate(matches, start=1):
            db.add(
                SimilarityMatch(
                    query_context=query_vector,
                    matched_event_id=m["event_id"],
                    reaction_id=m["id"],
                    similarity_score=m["similarity_score"],
                    rank=rank,
                    analysis_snapshot_id=analysis_snapshot_id,
                )
            )
        db.commit()
        return len(matches)
    except Exception:  # noqa: BLE001
        logger.exception("Persisting similarity matches failed; continuing.")
        db.rollback()
        return 0
