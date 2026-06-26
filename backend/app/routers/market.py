"""USD/MXN market data endpoints."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.database import get_db
from app.models import MarketSnapshot, NewsItem
from app.services.calendar import get_calendar_provider
from app.services.market_data import MarketData, get_market_data
from app.services.news import get_news_provider

router = APIRouter(prefix="/market", tags=["market"])


def store_news_items(db: Session, items: list[dict], provider: str = "mock") -> int:
    """Insert news items, skipping ones already stored (headline + published_at)."""
    stored = 0
    for item in items or []:
        headline = item.get("headline")
        if not headline:
            continue
        published_at = item.get("published_at")
        exists = db.execute(
            select(NewsItem.id)
            .where(NewsItem.headline == headline)
            .where(NewsItem.published_at == published_at)
            .limit(1)
        ).first()
        if exists:
            continue
        db.add(
            NewsItem(
                headline=headline,
                summary=item.get("summary", ""),
                source=item.get("source", ""),
                url=item.get("url", ""),
                published_at=published_at,
                sentiment=item.get("sentiment", "neutral"),
                affected_currencies=item.get("affected_currencies"),
                importance=item.get("importance", "low"),
                tags=item.get("tags"),
                provider=provider,
            )
        )
        stored += 1
    return stored


def capture_market_snapshot(db: Session) -> tuple[MarketSnapshot, MarketData, list[dict]]:
    """Fetch a fresh market snapshot (+ news + calendar) and persist it."""
    market = get_market_data()

    news_provider = get_news_provider()
    news = news_provider.get_news()

    calendar = get_calendar_provider().get_upcoming(limit=6)

    store_news_items(db, news, provider=news_provider.source)

    snapshot = MarketSnapshot(
        pair=market.pair,
        usdmxn=market.usdmxn,
        inverse_usdmxn=market.inverse_usdmxn,
        dxy=market.dxy,
        us2y=market.us2y,
        us10y=market.us10y,
        treasury_yield=market.treasury_yield,
        oil=market.oil,
        gold=market.gold,
        sp_futures=market.sp_futures,
        vix=market.vix,
        news=news,
        economic_calendar=calendar,
        provider=market.provider,
        source=market.source,
    )
    db.add(snapshot)
    db.commit()
    db.refresh(snapshot)
    return snapshot, market, news


def serialize_market(snapshot: MarketSnapshot) -> dict:
    return {
        "id": snapshot.id,
        "pair": snapshot.pair,
        "created_at": snapshot.created_at.isoformat() if snapshot.created_at else None,
        "usdmxn": snapshot.usdmxn,
        "inverse_usdmxn": snapshot.inverse_usdmxn,
        "dxy": snapshot.dxy,
        "us2y": snapshot.us2y,
        "us10y": snapshot.us10y,
        "treasury_yield": snapshot.treasury_yield,
        "oil": snapshot.oil,
        "gold": snapshot.gold,
        "sp_futures": snapshot.sp_futures,
        "vix": snapshot.vix,
        "news": snapshot.news,
        "economic_calendar": snapshot.economic_calendar,
        "provider": snapshot.provider,
        "source": snapshot.source,
    }


@router.get("/usdmxn")
def get_usdmxn(db: Session = Depends(get_db)) -> dict:
    """Fetch the current USD/MXN snapshot, store it, and return it."""
    snapshot, _, _ = capture_market_snapshot(db)
    return serialize_market(snapshot)


@router.get("/usdmxn/history")
def get_usdmxn_history(
    limit: int = Query(default=20, ge=1, le=200),
    db: Session = Depends(get_db),
) -> dict:
    """Return the most recent stored USD/MXN snapshots."""
    rows = db.execute(
        select(MarketSnapshot)
        .where(MarketSnapshot.pair == "USDMXN")
        .order_by(MarketSnapshot.created_at.desc())
        .limit(limit)
    ).scalars().all()
    return {"count": len(rows), "snapshots": [serialize_market(r) for r in rows]}
