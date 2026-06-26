"""News endpoints."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.database import get_db
from app.models import NewsItem
from app.routers.market import store_news_items
from app.services.context_builder import serialize_news_row
from app.services.news import get_news_provider

router = APIRouter(prefix="/news", tags=["news"])


@router.get("/recent")
def recent_news(
    limit: int = Query(default=20, ge=1, le=100),
    db: Session = Depends(get_db),
) -> dict:
    """Fetch fresh news (mock/live), store new items, return the most recent."""
    provider = get_news_provider()
    stored = store_news_items(db, provider.get_news(), provider=provider.source)
    if stored:
        db.commit()

    rows = db.execute(
        select(NewsItem).order_by(NewsItem.created_at.desc()).limit(limit)
    ).scalars().all()
    return {
        "count": len(rows),
        "provider": provider.source,
        "news": [serialize_news_row(r) for r in rows],
    }
