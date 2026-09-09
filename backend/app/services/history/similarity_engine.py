"""Similarity engine: find historical environments like the current one."""

from __future__ import annotations

import logging
import math

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import Settings, get_settings
from app.models import ResearchMarketSnapshot, SimilarityMatch
from app.services.cftc_positioning import current_cftc_positioning
from app.services.fx_options_repair import current_fx_options
from app.services.history.historical_events import ensure_history_seeded, load_reactions
from app.services.history.historical_snapshots import (
    COMPARABLE_MIN_SIMILARITY,
    has_research_snapshots,
    load_research_comparables,
    research_snapshot_bounds,
)
from app.services.intraday_repair import current_intraday_features
from app.services.signal_weights import event_signal_key, news_category

logger = logging.getLogger(__name__)

DEFAULT_SIMILARITY_WEIGHTS: dict[str, float] = {
    "regime": 0.18, "event_type": 0.18, "vix": 0.12, "dxy": 0.12,
    "rate_differential": 0.12, "spread_2y": 0.10, "spread_10y": 0.08,
    "us2y": 0.06, "us10y": 0.06, "oil": 0.08, "momentum": 0.08,
    "momentum_1h": 0.10, "momentum_2h": 0.10, "momentum_4h": 0.12,
    "intraday_vol_4h": 0.06, "intraday_vol_24h": 0.06,
    "iv_1w": 0.10, "iv_1m": 0.10, "rr25_1w": 0.12, "rr25_1m": 0.12,
    "butterfly_25d_1m": 0.06,
    "cftc_mxn_net_pct_oi": 0.12, "cftc_mxn_open_interest": 0.04,
    "news_tags": 0.06, "sp_futures": 0.04, "gold": 0.04,
}

_SCALES: dict[str, float] = {
    "vix": 6.0, "dxy": 3.0, "rate_differential": 1.0,
    "spread_2y": 1.0, "spread_10y": 1.0, "us2y": 0.7, "us10y": 0.6,
    "oil": 8.0, "momentum": 0.06,
    "momentum_1h": 0.12, "momentum_2h": 0.18, "momentum_4h": 0.28,
    "intraday_vol_4h": 0.08, "intraday_vol_24h": 0.08,
    "iv_1w": 2.0, "iv_1m": 2.0, "rr25_1w": 1.0, "rr25_1m": 1.0,
    "butterfly_25d_1m": 0.5,
    "cftc_mxn_net_pct_oi": 12.0, "cftc_mxn_open_interest": 40000.0,
    "sp_futures": 250.0, "gold": 120.0,
}

_NUMERIC = tuple(_SCALES.keys())


def get_similarity_weights(settings: Settings | None = None) -> dict[str, float]:
    settings = settings or get_settings()
    weights = dict(DEFAULT_SIMILARITY_WEIGHTS)
    override = getattr(settings, "similarity_weights", None)
    if isinstance(override, dict):
        for key, value in override.items():
            if key not in weights:
                logger.warning("Ignoring unknown similarity weight key: %r", key)
                continue
            try:
                weights[key] = float(value)
            except (TypeError, ValueError):
                logger.warning("Ignoring non-numeric similarity weight %r: %r", key, value)
    return weights


def _dominant_event_type(context: dict) -> str | None:
    events = (context.get("released_last_24h") or []) + (context.get("upcoming_events") or [])
    for importance in ("high", None):
        for ev in events:
            if importance and ev.get("importance") != importance:
                continue
            key = event_signal_key(ev.get("event", ""))
            if key:
                return key
    return None


def _news_tags(context: dict) -> list[str]:
    tags: set[str] = set()
    for item in context.get("recent_news") or []:
        for tag in item.get("tags") or []:
            tags.add(str(tag).lower())
        tags.add(news_category(item))
    return sorted(tags)


def build_feature_vector(context: dict, regime: dict | None = None) -> dict:
    market = context.get("market") or {}
    momentum = context.get("momentum") or {}
    regime = regime or context.get("market_regime") or {}
    keys = (
        "dxy", "us2y", "us10y", "mx2y", "mx10y", "spread_2y", "spread_10y",
        "oil", "gold", "vix", "sp_futures", "momentum_1h", "momentum_2h",
        "momentum_4h", "intraday_vol_4h", "intraday_vol_24h", "fed_funds",
        "banxico_rate", "rate_differential", "iv_1w", "iv_1m", "rr25_1w",
        "rr25_1m", "butterfly_25d_1m", "cftc_mxn_net", "cftc_mxn_net_pct_oi",
        "cftc_mxn_open_interest",
    )
    out = {key: market.get(key) for key in keys}
    out.update({
        "regime": (regime or {}).get("primary"),
        "event_type": _dominant_event_type(context),
        "momentum": momentum.get("change"),
        "news_tags": _news_tags(context),
    })
    return out


def _latest_research(db: Session) -> ResearchMarketSnapshot | None:
    return db.execute(select(ResearchMarketSnapshot).order_by(ResearchMarketSnapshot.trade_date.desc()).limit(1)).scalars().first()


def _inject_current_relative_rates(db: Session, query: dict) -> None:
    latest = _latest_research(db)
    if latest is None:
        return
    if query.get("rate_differential") is None and latest.fed_funds is not None and latest.banxico_rate is not None:
        query["fed_funds"] = float(latest.fed_funds)
        query["banxico_rate"] = float(latest.banxico_rate)
        query["rate_differential"] = float(latest.banxico_rate) - float(latest.fed_funds)
    if query.get("mx2y") is None and latest.mx2y is not None:
        query["mx2y"] = float(latest.mx2y)
    if query.get("mx10y") is None and latest.mx10y is not None:
        query["mx10y"] = float(latest.mx10y)
    if query.get("spread_2y") is None and query.get("mx2y") is not None and query.get("us2y") is not None:
        query["spread_2y"] = float(query["mx2y"]) - float(query["us2y"])
    if query.get("spread_10y") is None and query.get("mx10y") is not None and query.get("us10y") is not None:
        query["spread_10y"] = float(query["mx10y"]) - float(query["us10y"])


def _inject_current_intraday(db: Session, query: dict) -> None:
    for key, value in current_intraday_features(db).items():
        if query.get(key) is None:
            query[key] = value


def _inject_current_options(db: Session, query: dict) -> None:
    for key, value in current_fx_options(db).items():
        if query.get(key) is None:
            query[key] = value


def _inject_current_cftc(db: Session, query: dict) -> None:
    for key, value in current_cftc_positioning(db).items():
        if query.get(key) is None:
            query[key] = value


def _jaccard(a: list | None, b: list | None) -> float | None:
    sa, sb = set(a or []), set(b or [])
    if not sa and not sb:
        return None
    union = sa | sb
    return len(sa & sb) / len(union) if union else None


def score_reaction(query: dict, reaction: dict, weights: dict[str, float]) -> float:
    ctx = reaction.get("context") or {}
    total_w = acc = 0.0
    for key, qval, rval in (
        ("regime", query.get("regime"), ctx.get("regime")),
        ("event_type", query.get("event_type"), reaction.get("event_type")),
    ):
        if qval is None or rval is None:
            continue
        weight = weights.get(key, 0.0)
        if weight > 0:
            acc += weight * (1.0 if str(qval) == str(rval) else 0.0)
            total_w += weight
    for key in _NUMERIC:
        qv, rv = query.get(key), ctx.get(key)
        if qv is None or rv is None:
            continue
        weight = weights.get(key, 0.0)
        if weight <= 0:
            continue
        scale = _SCALES[key] or 1.0
        acc += weight * math.exp(-((float(qv) - float(rv)) / scale) ** 2)
        total_w += weight
    j = _jaccard(query.get("news_tags"), ctx.get("news_tags"))
    if j is not None and weights.get("news_tags", 0.0) > 0:
        weight = weights["news_tags"]
        acc += weight * j
        total_w += weight
    return round(acc / total_w, 4) if total_w > 0 else 0.0


def find_similar(db: Session, context: dict, regime: dict | None = None, top_n: int = 5,
                 persist: bool = False, analysis_snapshot_id: int | None = None,
                 settings: Settings | None = None) -> dict:
    settings = settings or get_settings()
    weights = get_similarity_weights(settings)
    query = build_feature_vector(context, regime=regime)
    _inject_current_relative_rates(db, query)
    _inject_current_intraday(db, query)
    _inject_current_options(db, query)
    _inject_current_cftc(db, query)
    ensure_history_seeded(db)
    use_research = has_research_snapshots(db)
    if use_research:
        bounds = research_snapshot_bounds(db)
        reactions = load_research_comparables(db)
        data_mode = "research_snapshots"
        since_year = int(str(bounds["start_date"])[:4]) if bounds.get("start_date") else None
    else:
        bounds, since_year, reactions, data_mode = {}, None, load_reactions(db), "event_reactions"
    scored = []
    for reaction in reactions:
        score = score_reaction(query, reaction, weights)
        item = dict(reaction)
        item["similarity_score"] = score
        item["distance_score"] = round(1.0 - score, 4)
        scored.append(item)
    scored.sort(key=lambda x: x["similarity_score"], reverse=True)
    comparable = [x for x in scored if x["similarity_score"] >= COMPARABLE_MIN_SIMILARITY]
    top = scored[:top_n]
    for rank, item in enumerate(top, start=1):
        item["rank"] = rank
    if persist and top:
        persist_matches(db, query, top, analysis_snapshot_id)
    best = top[0]["similarity_score"] if top else 0.0
    return {
        "query_vector": query, "weights": weights, "considered": len(reactions),
        "comparable_count": len(comparable), "database_size": bounds.get("total_snapshots") or len(reactions),
        "since_year": since_year, "data_mode": data_mode, "top_matches": top,
        "best_similarity": best, "best_distance": round(1.0 - best, 4) if top else None,
    }


def persist_matches(db: Session, query_vector: dict, matches: list[dict], analysis_snapshot_id: int | None = None) -> int:
    try:
        for rank, match in enumerate(matches, start=1):
            snapshot_id = match.get("id") if match.get("trade_date") else None
            db.add(SimilarityMatch(
                query_context=query_vector,
                matched_event_id=match.get("event_id") if not snapshot_id else None,
                research_snapshot_id=snapshot_id,
                reaction_id=match.get("id") if not snapshot_id else None,
                similarity_score=match["similarity_score"], rank=rank,
                analysis_snapshot_id=analysis_snapshot_id,
            ))
        db.commit()
        return len(matches)
    except Exception:  # noqa: BLE001
        logger.exception("Persisting similarity matches failed; continuing.")
        db.rollback()
        return 0
