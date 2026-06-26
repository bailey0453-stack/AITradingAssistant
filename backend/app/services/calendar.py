"""Economic calendar provider system.

Tracks the macro events that move USD/MXN. Mock provider is active by default;
implement `LiveCalendarProvider` (e.g. an economic-calendar API) and set
`CALENDAR_API_KEY` to go live. Output schema per event:

    event (name), country, release_time (ISO), forecast, previous, actual,
    importance ("high" | "medium" | "low"),
    currency_impact (e.g. "USD" | "MXN"),
    status ("upcoming" | "released")

Tracked events: US CPI, US PPI, NFP / jobs, GDP, Retail Sales, FOMC, Fed
speeches, Banxico, Mexico CPI, Mexico GDP, Mexico employment, Treasury auctions.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import datetime, timedelta, timezone


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat()


class CalendarProvider(ABC):
    source = "base"

    @abstractmethod
    def get_events(self) -> list[dict]:
        """Return all known events (mix of upcoming + recently released)."""
        raise NotImplementedError

    def get_upcoming(self, limit: int | None = None) -> list[dict]:
        events = [e for e in self.get_events() if e.get("status") == "upcoming"]
        events.sort(key=lambda e: e.get("release_time") or "")
        return events[:limit] if limit else events

    def get_recent_released(self, limit: int | None = None) -> list[dict]:
        events = [e for e in self.get_events() if e.get("status") == "released"]
        events.sort(key=lambda e: e.get("release_time") or "", reverse=True)
        return events[:limit] if limit else events


class MockCalendarProvider(CalendarProvider):
    source = "mock"

    def get_events(self) -> list[dict]:
        now = datetime.now(timezone.utc)

        def released(hours_ago, **kw):
            kw.setdefault("status", "released")
            kw["release_time"] = _iso(now - timedelta(hours=hours_ago))
            return kw

        def upcoming(days_ahead, **kw):
            kw.setdefault("status", "upcoming")
            kw.setdefault("actual", None)
            kw["release_time"] = _iso(now + timedelta(days=days_ahead))
            return kw

        return [
            # --- recently released ---
            released(
                2,
                event="US CPI (MoM)",
                country="US",
                forecast="0.3%",
                previous="0.4%",
                actual="0.4%",
                importance="high",
                currency_impact="USD",
            ),
            released(
                26,
                event="US NFP / Nonfarm Payrolls",
                country="US",
                forecast="190K",
                previous="175K",
                actual="206K",
                importance="high",
                currency_impact="USD",
            ),
            released(
                50,
                event="Mexico CPI (MoM)",
                country="MX",
                forecast="0.2%",
                previous="0.3%",
                actual="0.1%",
                importance="medium",
                currency_impact="MXN",
            ),
            # --- upcoming ---
            upcoming(
                1,
                event="US PPI (MoM)",
                country="US",
                forecast="0.2%",
                previous="0.5%",
                importance="medium",
                currency_impact="USD",
            ),
            upcoming(
                2,
                event="US Retail Sales (MoM)",
                country="US",
                forecast="0.3%",
                previous="0.1%",
                importance="high",
                currency_impact="USD",
            ),
            upcoming(
                3,
                event="Fed Chair Speech",
                country="US",
                forecast=None,
                previous=None,
                importance="medium",
                currency_impact="USD",
            ),
            upcoming(
                4,
                event="US 10Y Treasury Auction",
                country="US",
                forecast=None,
                previous=None,
                importance="medium",
                currency_impact="USD",
            ),
            upcoming(
                5,
                event="Banxico Rate Decision",
                country="MX",
                forecast="hold (11.00%)",
                previous="11.00%",
                importance="high",
                currency_impact="MXN",
            ),
            upcoming(
                7,
                event="Mexico GDP (QoQ)",
                country="MX",
                forecast="0.3%",
                previous="0.2%",
                importance="medium",
                currency_impact="MXN",
            ),
            upcoming(
                9,
                event="Mexico Employment / Unemployment Rate",
                country="MX",
                forecast="2.7%",
                previous="2.6%",
                importance="medium",
                currency_impact="MXN",
            ),
            upcoming(
                12,
                event="FOMC Meeting",
                country="US",
                forecast="hold",
                previous="hold",
                importance="high",
                currency_impact="USD",
            ),
            upcoming(
                14,
                event="US GDP (QoQ, advance)",
                country="US",
                forecast="2.0%",
                previous="1.4%",
                importance="high",
                currency_impact="USD",
            ),
        ]


class LiveCalendarProvider(CalendarProvider):  # pragma: no cover - stub
    source = "live"

    def __init__(self, settings) -> None:
        self.settings = settings

    def get_events(self) -> list[dict]:
        # TODO: call an economic-calendar API and normalize to the schema above.
        raise NotImplementedError("LiveCalendarProvider not implemented yet.")


def get_calendar_provider(settings=None) -> CalendarProvider:
    from app.config import get_settings

    settings = settings or get_settings()
    if settings.is_mock or not settings.calendar_api_key:
        return MockCalendarProvider()
    return LiveCalendarProvider(settings)
