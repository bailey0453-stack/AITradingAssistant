"""Economic calendar provider system.

Tracks the macro events that move USD/MXN. Output schema per event:

    event (name), country, release_time (ISO), forecast, previous, actual,
    importance ("high" | "medium" | "low"),
    currency_impact (e.g. "USD" | "MXN"),
    status ("upcoming" | "released"),
    source (provider label, e.g. "FRED", "U.S. Treasury", "mock")

Tracked events: US CPI, US PPI, NFP / jobs, GDP, Retail Sales, FOMC, Fed
speeches, Treasury auctions, Banxico, Mexico CPI, Mexico GDP, Mexico employment.

Providers
---------
- ``MockCalendarProvider``              — realistic offline data; explicit mock mode.
- ``CompositeOfficialCalendarProvider`` — free official feeds (FRED + Treasury).
- ``TradingEconomicsCalendarProvider``  — paid live feed (Trading Economics).
- ``ResilientCalendarProvider``         — wraps a live provider; no mock fallback.

Selection (``get_calendar_provider``):
- CSV import when ``CALENDAR_CSV_PATH`` is set.
- Paid API when ``CALENDAR_API_KEY`` is set and provider is tradingeconomics/finnhub.
- Official free calendars when ``FRED_API_KEY`` is set and mock mode is off.
- Mock only when ``USE_MOCK_DATA=true`` (or explicit mock provider).
- Empty/error provider when live is wanted but nothing is configured.
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from datetime import datetime, timedelta, timezone

import httpx

from app.config import Settings, get_settings
from app.services.secrets import scrub

logger = logging.getLogger(__name__)


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat()


class CalendarProvider(ABC):
    source = "base"

    @property
    def status(self) -> str:
        """Provider health: LIVE | PARTIAL | MOCK | ERROR."""
        if self.source == "mock":
            return "MOCK"
        if self.source in {"live", "imported", "official"}:
            return "LIVE"
        if self.source == "fallback":
            return "MOCK"
        if self.source == "error":
            return "ERROR"
        return "ERROR"

    @property
    def coverage_gaps(self) -> list[dict]:
        """Event types not available from configured free sources."""
        return []

    @abstractmethod
    def get_events(self) -> list[dict]:
        """Return all known events (mix of upcoming + recently released)."""
        raise NotImplementedError

    def get_upcoming(self, limit: int | None = None) -> list[dict]:
        events = [
            e for e in self.get_events()
            if e.get("status") == "upcoming"
        ]
        events.sort(key=lambda e: e.get("release_time") or "")
        return events[:limit] if limit else events

    def get_recent_released(self, limit: int | None = None) -> list[dict]:
        events = [
            e for e in self.get_events()
            if e.get("status") == "released"
        ]
        events.sort(key=lambda e: e.get("release_time") or "", reverse=True)
        return events[:limit] if limit else events


class MockCalendarProvider(CalendarProvider):
    source = "mock"

    @property
    def status(self) -> str:
        return "MOCK"

    def get_events(self) -> list[dict]:
        now = datetime.now(timezone.utc)

        def released(hours_ago, **kw):
            kw.setdefault("status", "released")
            kw.setdefault("source", "mock")
            kw["release_time"] = _iso(now - timedelta(hours=hours_ago))
            return kw

        def upcoming(days_ahead, **kw):
            kw.setdefault("status", "upcoming")
            kw.setdefault("actual", None)
            kw.setdefault("source", "mock")
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


class TradingEconomicsCalendarProvider(CalendarProvider):
    """Live economic calendar via Trading Economics.

    Fetches a US + Mexico window (recent past through near future) and maps it
    to the shared schema. The API key is passed as the ``c`` query parameter
    (Trading Economics' scheme); it is kept out of logs by scrubbing every
    outbound error string. Raises on any failure so the resilient wrapper can
    fall back to mock data.
    """

    source = "tradingeconomics"
    DEFAULT_BASE_URL = "https://api.tradingeconomics.com/calendar/country/united states,mexico"
    _IMPORTANCE = {3: "high", 2: "medium", 1: "low"}

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.base_url = settings.calendar_base_url or self.DEFAULT_BASE_URL
        self.timeout = settings.http_timeout_seconds

    def get_events(self) -> list[dict]:
        if not self.settings.calendar_api_key:
            raise RuntimeError("CALENDAR_API_KEY is not configured.")

        now = datetime.now(timezone.utc)
        params = {
            "c": self.settings.calendar_api_key,
            "f": "json",
            "d1": (now - timedelta(days=3)).strftime("%Y-%m-%d"),
            "d2": (now + timedelta(days=21)).strftime("%Y-%m-%d"),
        }
        try:
            resp = httpx.get(self.base_url, params=params, timeout=self.timeout)
            resp.raise_for_status()
            data = resp.json()
        except Exception as exc:  # noqa: BLE001 - re-raise scrubbed
            raise RuntimeError(
                f"Calendar request failed: "
                f"{scrub(str(exc), self.settings.calendar_api_key)}"
            ) from None

        if not isinstance(data, list):
            raise RuntimeError("Calendar provider returned an unexpected payload.")

        events = [self._map_event(row, now) for row in data]
        events = [e for e in events if e]
        if not events:
            raise RuntimeError("Calendar provider returned no usable events.")
        return events

    def _map_event(self, row: dict, now: datetime) -> dict | None:
        if not isinstance(row, dict):
            return None
        name = (row.get("Event") or row.get("Category") or "").strip()
        if not name:
            return None
        country = (row.get("Country") or "").strip()
        currency_impact = "MXN" if country.lower() == "mexico" else "USD"

        raw_date = row.get("Date")
        release_time = self._parse_dt(raw_date)
        actual = _clean(row.get("Actual"))
        status = "released" if actual not in (None, "") else "upcoming"

        return {
            "event": name,
            "country": "MX" if currency_impact == "MXN" else "US",
            "release_time": release_time,
            "forecast": _clean(row.get("Forecast")) or _clean(row.get("TEForecast")),
            "previous": _clean(row.get("Previous")),
            "actual": actual,
            "importance": self._IMPORTANCE.get(_as_int(row.get("Importance")), "low"),
            "currency_impact": currency_impact,
            "status": status,
            "source": "Trading Economics",
        }

    @staticmethod
    def _parse_dt(raw) -> str | None:
        if not raw:
            return None
        text = str(raw).replace("Z", "+00:00")
        try:
            dt = datetime.fromisoformat(text)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return _iso(dt)
        except ValueError:
            return str(raw)


class CSVCalendarProvider(CalendarProvider):
    """Importable calendar from a local CSV export (no API key required).

    Point ``CALENDAR_CSV_PATH`` at a CSV with a header row containing any of:
    ``event,country,release_time,forecast,previous,actual,importance,
    currency_impact`` (``release_time`` ISO-8601). ``status`` is derived from
    whether ``actual`` is present; ``importance`` accepts ``high|medium|low`` or
    ``3|2|1``. Raises on any problem so the resilient wrapper falls back to mock.
    """

    source = "imported"
    _IMPORTANCE_WORDS = {"3": "high", "2": "medium", "1": "low"}

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.path = settings.calendar_csv_path

    def get_events(self) -> list[dict]:
        import csv
        import os

        if not self.path:
            raise RuntimeError("CALENDAR_CSV_PATH is not configured.")
        if not os.path.isfile(self.path):
            raise RuntimeError(f"Calendar CSV not found: {self.path}")

        events: list[dict] = []
        with open(self.path, newline="", encoding="utf-8") as fh:
            for row in csv.DictReader(fh):
                name = (row.get("event") or row.get("Event") or "").strip()
                if not name:
                    continue
                country = (row.get("country") or row.get("Country") or "US").strip()
                currency_impact = (
                    row.get("currency_impact")
                    or ("MXN" if country.upper() in {"MX", "MEXICO"} else "USD")
                ).strip().upper()
                actual = _clean(row.get("actual") or row.get("Actual"))
                imp_raw = (row.get("importance") or row.get("Importance") or "").strip().lower()
                importance = self._IMPORTANCE_WORDS.get(imp_raw, imp_raw or "low")
                if importance not in {"high", "medium", "low"}:
                    importance = "low"
                events.append({
                    "event": name,
                    "country": "MX" if currency_impact == "MXN" else "US",
                    "release_time": TradingEconomicsCalendarProvider._parse_dt(
                        row.get("release_time") or row.get("Date")
                    ),
                    "forecast": _clean(row.get("forecast") or row.get("Forecast")),
                    "previous": _clean(row.get("previous") or row.get("Previous")),
                    "actual": actual,
                    "importance": importance,
                    "currency_impact": currency_impact,
                    "status": "released" if actual not in (None, "") else "upcoming",
                    "source": _clean(row.get("source") or row.get("Source")) or "imported CSV",
                })
        if not events:
            raise RuntimeError("Calendar CSV contained no usable events.")
        return events


class FinnhubCalendarProvider(CalendarProvider):  # pragma: no cover - future stub
    source = "finnhub"

    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    def get_events(self) -> list[dict]:
        raise NotImplementedError("Finnhub calendar provider not implemented yet.")


def _clean(value) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _as_int(value) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 1


# Registry so new providers plug in via CALENDAR_PROVIDER without touching callers.
_LIVE_CALENDAR_PROVIDERS = {
    "tradingeconomics": TradingEconomicsCalendarProvider,
    "finnhub": FinnhubCalendarProvider,
    "csv": CSVCalendarProvider,
}


class EmptyCalendarProvider(CalendarProvider):
    """No calendar configured — returns empty events with ERROR status."""

    source = "error"

    @property
    def status(self) -> str:
        return "ERROR"

    def get_events(self) -> list[dict]:
        return []


class ResilientCalendarProvider(CalendarProvider):
    """Wraps a live provider; does not fabricate mock events on failure.

    ``.source`` is ``"live"`` / ``"imported"`` after a successful fetch,
    otherwise ``"error"``. Results are cached for the life of the instance.
    """

    def __init__(self, settings: Settings, *, fallback_to_mock: bool = False) -> None:
        self.settings = settings
        self.source = "error"
        self._status = "ERROR"
        self._fallback_to_mock = fallback_to_mock
        live_cls = _LIVE_CALENDAR_PROVIDERS.get(
            (settings.calendar_provider or "tradingeconomics").lower(),
            TradingEconomicsCalendarProvider,
        )
        self._live = live_cls(settings)
        self._success_source = getattr(self._live, "source", "live")
        if self._success_source not in {"imported"}:
            self._success_source = "live"
        self._mock = MockCalendarProvider()
        self._cache: list[dict] | None = None

    @property
    def status(self) -> str:
        return self._status

    def get_events(self) -> list[dict]:
        if self._cache is not None:
            return self._cache
        try:
            events = self._live.get_events()
            self.source = self._success_source
            self._status = "LIVE"
        except Exception as exc:  # noqa: BLE001 - degrade gracefully
            logger.warning(
                "Live calendar fetch failed (%s).",
                scrub(str(exc), self.settings.calendar_api_key),
            )
            if self._fallback_to_mock:
                self.source = "fallback"
                self._status = "MOCK"
                events = self._mock.get_events()
            else:
                self.source = "error"
                self._status = "ERROR"
                events = []
        self._cache = events
        return events


def get_calendar_provider(settings: Settings | None = None) -> CalendarProvider:
    settings = settings or get_settings()
    provider_name = (settings.calendar_provider or "auto").lower()

    if settings.calendar_csv_enabled:
        return ResilientCalendarProvider(settings, fallback_to_mock=False)

    if provider_name == "mock":
        return MockCalendarProvider()

    if settings.is_mock:
        return MockCalendarProvider()

    if provider_name in ("tradingeconomics", "finnhub") and settings.calendar_api_key:
        return ResilientCalendarProvider(settings, fallback_to_mock=False)

    if settings.fred_api_key and provider_name not in ("mock", "csv"):
        from app.services.official_calendar import CompositeOfficialCalendarProvider

        return CompositeOfficialCalendarProvider(settings)

    if settings.calendar_api_key:
        return ResilientCalendarProvider(settings, fallback_to_mock=False)

    return EmptyCalendarProvider()
