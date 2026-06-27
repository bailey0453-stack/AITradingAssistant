"""Context builder for the USD/MXN intelligence engine.

Gathers everything the analysis engine needs into one structured object:
current market snapshot, recent news, upcoming + recently-released economic
events, and recent AI analyses. Also builds a human-readable event timeline
(CPI released, DXY moved, USD/MXN moved, AI signal change, momentum change).

All DB reads degrade gracefully — a query failure yields an empty list so the
analysis endpoint never breaks.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import Settings, get_settings
from app.models import AnalysisSnapshot, MarketSnapshot, NewsItem
from app.services.calendar import get_calendar_provider
from app.services.market_data import MarketData

logger = logging.getLogger(__name__)


def _safe(db_call, default):
    try:
        return db_call()
    except Exception:  # noqa: BLE001
        logger.exception("context_builder DB read failed; using empty default.")
        return default


def serialize_news_row(row: NewsItem) -> dict:
    return {
        "id": row.id,
        "headline": row.headline,
        "summary": row.summary,
        "source": row.source,
        "url": row.url,
        "published_at": row.published_at,
        "sentiment": row.sentiment,
        "affected_currencies": row.affected_currencies,
        "importance": row.importance,
        "relevance_score": row.relevance_score,
        "tags": row.tags,
    }


def _brief_analysis(row: AnalysisSnapshot) -> dict:
    return {
        "id": row.id,
        "created_at": row.created_at.isoformat() if row.created_at else None,
        "direction": row.direction,
        "trade_score": row.trade_score,
        "confidence": row.confidence,
        "momentum_status": row.momentum_status,
    }


def recent_news_rows(db: Session, limit: int = 8) -> list[NewsItem]:
    return _safe(
        lambda: db.execute(
            select(NewsItem).order_by(NewsItem.created_at.desc()).limit(limit)
        ).scalars().all(),
        [],
    )


def recent_analyses_rows(db: Session, limit: int = 5) -> list[AnalysisSnapshot]:
    return _safe(
        lambda: db.execute(
            select(AnalysisSnapshot)
            .order_by(AnalysisSnapshot.created_at.desc())
            .limit(limit)
        ).scalars().all(),
        [],
    )


def recent_market_rows(db: Session, limit: int = 2) -> list[MarketSnapshot]:
    return _safe(
        lambda: db.execute(
            select(MarketSnapshot)
            .order_by(MarketSnapshot.created_at.desc())
            .limit(limit)
        ).scalars().all(),
        [],
    )


def build_context(
    db: Session,
    market: MarketData,
    fresh_news: list[dict] | None = None,
    settings: Settings | None = None,
) -> dict:
    """Assemble the structured context object used by the analysis engine."""
    settings = settings or get_settings()
    cal = get_calendar_provider(settings)

    upcoming = _safe(lambda: cal.get_upcoming(limit=8), [])
    released = _safe(lambda: cal.get_recent_released(limit=8), [])
    released_24h = _within_last_24h(released)

    db_news = [serialize_news_row(r) for r in recent_news_rows(db, limit=16)]
    recent_news = _dedupe_news(db_news or list(fresh_news or []))[:8]
    recent_analyses = [_brief_analysis(r) for r in recent_analyses_rows(db, limit=5)]
    momentum = _compute_momentum(db)

    return {
        "market": market.to_dict(),
        "recent_news": recent_news,
        "upcoming_events": upcoming,
        "released_events": released,
        "released_last_24h": released_24h,
        "recent_analyses": recent_analyses,
        "momentum": momentum,
        "calendar_source": getattr(cal, "source", "mock"),
    }


def _compute_momentum(db: Session) -> dict | None:
    """USD/MXN change between the two most recent stored snapshots.

    Returns None until at least two snapshots exist, so momentum never fires on
    a synthetic baseline — only on real consecutive observations.
    """
    snaps = recent_market_rows(db, limit=2)
    if len(snaps) < 2:
        return None
    latest, prev = snaps[0], snaps[1]
    if latest.usdmxn is None or prev.usdmxn is None:
        return None
    return {
        "change": round(latest.usdmxn - prev.usdmxn, 4),
        "from": prev.usdmxn,
        "to": latest.usdmxn,
    }


def _dedupe_news(items: list[dict]) -> list[dict]:
    """Drop repeated headlines (mock re-runs restamp published_at each call)."""
    seen: set[str] = set()
    out: list[dict] = []
    for item in items or []:
        key = (item.get("headline") or "").strip().lower()
        if key and key in seen:
            continue
        seen.add(key)
        out.append(item)
    return out


def _within_last_24h(events: list[dict]) -> list[dict]:
    """Filter released events to those within the last 24 hours."""
    cutoff = datetime.now(timezone.utc) - timedelta(hours=24)
    out: list[dict] = []
    for ev in events or []:
        rt = ev.get("release_time")
        if not rt:
            continue
        try:
            when = datetime.fromisoformat(str(rt).replace("Z", "+00:00"))
        except ValueError:
            continue
        if when.tzinfo is None:
            when = when.replace(tzinfo=timezone.utc)
        if when >= cutoff:
            out.append(ev)
    return out


def build_timeline(db: Session, context: dict) -> list[dict]:
    """Build a recent-context timeline (most recent first)."""
    timeline: list[dict] = []

    # 1. Recently released economic events.
    for ev in context.get("released_events", [])[:4]:
        timeline.append(
            {
                "time": ev.get("release_time"),
                "type": "event",
                "label": f"{ev.get('event')} released",
                "detail": (
                    f"actual {ev.get('actual')} vs forecast {ev.get('forecast')} "
                    f"(prev {ev.get('previous')})"
                ),
                "importance": ev.get("importance"),
            }
        )

    # 2. Market moves from the two most recent snapshots.
    snaps = recent_market_rows(db, limit=2)
    if len(snaps) >= 2:
        latest, prev = snaps[0], snaps[1]
        when = latest.created_at.isoformat() if latest.created_at else None
        if latest.usdmxn is not None and prev.usdmxn is not None:
            move = round(latest.usdmxn - prev.usdmxn, 4)
            timeline.append(
                {
                    "time": when,
                    "type": "market_move",
                    "label": "USD/MXN moved",
                    "detail": f"{prev.usdmxn} -> {latest.usdmxn} ({move:+.4f})",
                }
            )
        if latest.dxy is not None and prev.dxy is not None:
            dmove = round(latest.dxy - prev.dxy, 3)
            timeline.append(
                {
                    "time": when,
                    "type": "market_move",
                    "label": "DXY moved",
                    "detail": f"{prev.dxy} -> {latest.dxy} ({dmove:+.3f})",
                }
            )

    # 3. Notable recent news.
    for item in context.get("recent_news", []):
        if str(item.get("importance")) in {"high", "medium"}:
            timeline.append(
                {
                    "time": item.get("published_at"),
                    "type": "news",
                    "label": item.get("headline"),
                    "detail": f"{item.get('source', '')} · {item.get('sentiment', '')}",
                    "importance": item.get("importance"),
                }
            )

    # 4. AI signal / momentum changes from the two most recent analyses.
    analyses = context.get("recent_analyses", [])
    if len(analyses) >= 2:
        latest, prev = analyses[0], analyses[1]
        if latest["direction"] != prev["direction"]:
            verb = _signal_verb(prev["direction"], latest["direction"])
            timeline.append(
                {
                    "time": latest.get("created_at"),
                    "type": "signal_change",
                    "label": f"AI {verb} signal",
                    "detail": f"{prev['direction']} -> {latest['direction']}",
                }
            )
        if latest["momentum_status"] != prev["momentum_status"]:
            timeline.append(
                {
                    "time": latest.get("created_at"),
                    "type": "momentum_change",
                    "label": "Momentum changed",
                    "detail": f"{prev['momentum_status']} -> {latest['momentum_status']}",
                }
            )

    # 5. Next upcoming high-impact event.
    for ev in context.get("upcoming_events", []):
        if ev.get("importance") == "high":
            timeline.append(
                {
                    "time": ev.get("release_time"),
                    "type": "event_upcoming",
                    "label": f"Upcoming: {ev.get('event')}",
                    "detail": f"forecast {ev.get('forecast')}",
                    "importance": ev.get("importance"),
                }
            )
            break

    timeline.sort(key=lambda e: e.get("time") or "", reverse=True)
    return timeline[:12]


def _signal_verb(old: str, new: str) -> str:
    rank = {"SELL_USD": -1, "NO_TRADE": 0, "BUY_USD": 1}
    return "upgraded" if rank.get(new, 0) > rank.get(old, 0) else "downgraded"
