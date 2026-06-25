"""USD/MXN market data endpoints."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.database import get_db
from app.models import MarketSnapshot
from app.services.market_data import MarketData, get_market_provider
from app.services.news import get_news_provider

router = APIRouter(prefix="/market", tags=["market"])


def capture_market_snapshot(db: Session) -> tuple[MarketSnapshot, MarketData, list[dict]]:
    """Fetch a fresh market snapshot (+ news) and persist it."""
    market = get_market_provider().get_usdmxn()
    news_provider = get_news_provider()
    news = news_provider.get_news()
    calendar = news_provider.get_economic_calendar()

    snapshot = MarketSnapshot(
        pair=market.pair,
        usdmxn=market.usdmxn,
        dxy=market.dxy,
        treasury_yield=market.treasury_yield,
        oil=market.oil,
        news=news,
        economic_calendar=calendar,
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
        "dxy": snapshot.dxy,
        "treasury_yield": snapshot.treasury_yield,
        "oil": snapshot.oil,
        "news": snapshot.news,
        "economic_calendar": snapshot.economic_calendar,
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
