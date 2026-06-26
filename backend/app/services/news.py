"""Modular news provider system.

Returns structured news items so the analysis engine and stored snapshots have a
realistic shape. Mock provider is active by default; implement `LiveNewsProvider`
(e.g. a news API) and set `NEWS_API_KEY` to go live. Output schema per item:

    headline, summary, source, url, published_at (ISO),
    sentiment ("usd_bullish" | "mxn_bullish" | "neutral"),
    affected_currencies (list, e.g. ["USD", "MXN"]),
    importance ("high" | "medium" | "low"),
    tags (list)
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import datetime, timedelta, timezone

from app.config import Settings, get_settings


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat()


class NewsProvider(ABC):
    source = "base"

    @abstractmethod
    def get_news(self) -> list[dict]:
        raise NotImplementedError


class MockNewsProvider(NewsProvider):
    source = "mock"

    def get_news(self) -> list[dict]:
        now = datetime.now(timezone.utc)
        return [
            {
                "headline": "US yields tick higher after firm jobs data",
                "summary": "Stronger-than-expected payrolls lifted front-end yields, "
                "supporting the dollar broadly.",
                "source": "MockWire",
                "url": "https://example.com/news/us-yields",
                "published_at": _iso(now - timedelta(hours=1)),
                "sentiment": "usd_bullish",
                "affected_currencies": ["USD", "MXN"],
                "importance": "high",
                "tags": ["USD", "rates", "jobs"],
            },
            {
                "headline": "Banxico holds rate, signals data-dependent stance",
                "summary": "Mexico's central bank kept policy steady, citing sticky "
                "core inflation; tone seen broadly neutral for MXN.",
                "source": "MockWire",
                "url": "https://example.com/news/banxico",
                "published_at": _iso(now - timedelta(hours=3)),
                "sentiment": "neutral",
                "affected_currencies": ["MXN"],
                "importance": "medium",
                "tags": ["MXN", "central-bank"],
            },
            {
                "headline": "Oil firms as risk appetite improves",
                "summary": "Crude gained on improving global demand sentiment, a mild "
                "tailwind for the peso.",
                "source": "MockWire",
                "url": "https://example.com/news/oil",
                "published_at": _iso(now - timedelta(hours=5)),
                "sentiment": "mxn_bullish",
                "affected_currencies": ["MXN"],
                "importance": "low",
                "tags": ["oil", "risk"],
            },
        ]


class LiveNewsProvider(NewsProvider):  # pragma: no cover - stub
    source = "live"

    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    def get_news(self) -> list[dict]:
        # TODO: call a news API, normalize into the schema above, and tag
        # sentiment / affected_currencies / importance.
        raise NotImplementedError("LiveNewsProvider not implemented yet.")


def get_news_provider(settings: Settings | None = None) -> NewsProvider:
    settings = settings or get_settings()
    if settings.is_mock or not settings.news_api_key:
        return MockNewsProvider()
    return LiveNewsProvider(settings)
