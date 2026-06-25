"""News and economic-calendar providers (placeholders).

Returns mocked headlines + calendar events so the analysis engine and stored
snapshots have realistic shape. Swap in a real news API / calendar feed later.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import date, timedelta

from app.config import Settings, get_settings


class NewsProvider(ABC):
    source = "base"

    @abstractmethod
    def get_news(self) -> list[dict]:
        raise NotImplementedError

    @abstractmethod
    def get_economic_calendar(self) -> list[dict]:
        raise NotImplementedError


class MockNewsProvider(NewsProvider):
    source = "mock"

    def get_news(self) -> list[dict]:
        return [
            {
                "headline": "Banxico holds rate, signals data-dependent stance",
                "sentiment": "neutral",
                "impact": "medium",
                "tags": ["MXN", "central-bank"],
            },
            {
                "headline": "US yields tick higher on firm jobs data",
                "sentiment": "usd_bullish",
                "impact": "high",
                "tags": ["USD", "rates"],
            },
            {
                "headline": "Oil steadies as risk appetite improves",
                "sentiment": "mxn_bullish",
                "impact": "low",
                "tags": ["oil", "risk"],
            },
        ]

    def get_economic_calendar(self) -> list[dict]:
        today = date.today()
        return [
            {
                "date": str(today + timedelta(days=1)),
                "event": "US CPI (MoM)",
                "importance": "high",
                "forecast": "0.3%",
            },
            {
                "date": str(today + timedelta(days=2)),
                "event": "Mexico Industrial Production",
                "importance": "medium",
                "forecast": "0.1%",
            },
            {
                "date": str(today + timedelta(days=5)),
                "event": "Banxico Rate Decision",
                "importance": "high",
                "forecast": "hold",
            },
        ]


class LiveNewsProvider(NewsProvider):  # pragma: no cover - stub
    source = "live"

    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    def get_news(self) -> list[dict]:
        raise NotImplementedError("LiveNewsProvider not implemented yet.")

    def get_economic_calendar(self) -> list[dict]:
        raise NotImplementedError("LiveNewsProvider not implemented yet.")


def get_news_provider(settings: Settings | None = None) -> NewsProvider:
    settings = settings or get_settings()
    if settings.is_mock or not settings.news_api_key:
        return MockNewsProvider()
    return LiveNewsProvider(settings)
