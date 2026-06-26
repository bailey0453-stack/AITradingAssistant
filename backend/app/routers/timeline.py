"""Event timeline endpoint (read-only)."""

from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.database import get_db
from app.services.context_builder import build_context, build_timeline
from app.services.market_data import get_market_data
from app.services.news import get_news_provider

router = APIRouter(prefix="/timeline", tags=["timeline"])


@router.get("/usdmxn")
def usdmxn_timeline(db: Session = Depends(get_db)) -> dict:
    """Recent-context timeline: released events, market moves, news, signal changes.

    Read-only: builds from already-stored snapshots/analyses plus live providers
    without persisting anything.
    """
    market = get_market_data()
    fresh_news = get_news_provider().get_news()
    context = build_context(db, market, fresh_news=fresh_news)
    timeline = build_timeline(db, context)
    return {"pair": "USDMXN", "count": len(timeline), "timeline": timeline}
