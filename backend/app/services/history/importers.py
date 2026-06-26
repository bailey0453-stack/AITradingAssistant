"""Import framework for historical backfill (Phase 4).

Every data source sits behind the same ``HistoricalImporter`` interface, so the
orchestration (`run`) is shared and providers only implement *where the data
comes from*:

  - ``fetch_events()``           -> list of event dicts
  - ``fetch_price_path(event)``  -> [(hours_after_event, usdmxn_price), ...]

Implemented now:
  - ``MockSampleImporter`` — a self-contained sample of historical events with
    deterministically-generated USD/MXN reaction paths. **No API key, no paid
    provider, no network.** This is what seeds the system today.

Stubs (modular, ready to implement when keys/files are available):
  - ``CSVImporter``, ``YahooFinanceImporter``, ``FREDImporter``,
    ``AlphaVantageImporter``, ``PolygonImporter``.

Provenance is recorded on every row (``source`` + ``source_quality``) so a
reconstructed sample path is never confused with a vendor's real intraday tick.
"""

from __future__ import annotations

import logging
import random
from abc import ABC, abstractmethod
from datetime import datetime, timedelta, timezone

from sqlalchemy.orm import Session

from app.models import HistoricalEvent, HistoricalEventReaction, HistoricalMarketSnapshot
from app.services.history.historical_prices import compute_reaction_windows

logger = logging.getLogger(__name__)

# Typical surprise standard deviation per event type (for surprise_z scaling).
_SURPRISE_SIGMA: dict[str, float] = {
    "us_cpi": 0.2,
    "us_ppi": 0.3,
    "us_nfp": 60.0,      # thousands of jobs
    "us_gdp": 0.5,
    "fed_rate_decision": 0.25,
    "banxico_rate_decision": 0.25,
    "mexico_cpi": 0.2,
    "mexico_gdp": 0.4,
}

_IMPORTANCE_FACTOR = {"high": 1.0, "medium": 0.65, "low": 0.4}

# Path sample offsets (hours after the event). Covers the 6 reaction windows
# plus intermediate points so MFE/MAE/time-to-peak have texture.
_PATH_OFFSETS = [0.0, 0.25, 0.5, 1.0, 2.0, 4.0, 8.0, 24.0, 48.0, 72.0, 96.0, 120.0]


def _dt(y: int, m: int, d: int, h: int = 13) -> datetime:
    return datetime(y, m, d, h, 30, tzinfo=timezone.utc)


def _event_lean(event: dict) -> str:
    """USD+ / MXN+ lean from actual vs forecast and currency impact."""
    actual = event.get("actual")
    forecast = event.get("forecast")
    if actual is None or forecast is None or actual == forecast:
        return "USD+"  # neutral default; magnitude handles flat cases
    beat = actual > forecast
    impact = (event.get("currency_impact") or "USD").upper()
    if impact == "USD":
        return "USD+" if beat else "MXN+"
    return "MXN+" if beat else "USD+"


class HistoricalImporter(ABC):
    """Base importer: subclasses supply data, this class persists + computes."""

    name = "base"
    source_quality = "unknown"

    @abstractmethod
    def fetch_events(self) -> list[dict]:
        raise NotImplementedError

    @abstractmethod
    def fetch_price_path(self, event: dict) -> list[tuple[float, float]]:
        """Return [(hours_after_event, usdmxn_price)] including (0.0, baseline)."""
        raise NotImplementedError

    def run(self, db: Session) -> dict:
        """Import events + reaction paths into the historical tables."""
        events = self.fetch_events()
        n_events = n_reactions = n_points = 0

        for ev in events:
            sigma = _SURPRISE_SIGMA.get(ev["event_type"], 1.0) or 1.0
            actual, forecast = ev.get("actual"), ev.get("forecast")
            surprise = (
                round(actual - forecast, 4)
                if actual is not None and forecast is not None
                else None
            )
            surprise_z = round(surprise / sigma, 3) if surprise is not None else None

            event_row = HistoricalEvent(
                event_type=ev["event_type"],
                event_name=ev.get("event_name", ev["event_type"]),
                country=ev.get("country", "US"),
                release_time=ev["release_time"],
                forecast=forecast,
                actual=actual,
                previous=ev.get("previous"),
                surprise=surprise,
                surprise_z=surprise_z,
                importance=ev.get("importance", "medium"),
                currency_impact=ev.get("currency_impact", "USD"),
                source=self.name,
                source_quality=self.source_quality,
            )
            db.add(event_row)
            db.flush()  # assign id

            path = self.fetch_price_path(ev)
            baseline = path[0][1] if path else ev.get("baseline")
            ctx = ev.get("context", {})
            release = ev["release_time"]
            for hours, price in path:
                db.add(
                    HistoricalMarketSnapshot(
                        series="USDMXN",
                        ts=release + timedelta(hours=hours),
                        usdmxn=round(price, 4),
                        dxy=ctx.get("dxy"),
                        us2y=ctx.get("us2y"),
                        us10y=ctx.get("us10y"),
                        oil=ctx.get("oil"),
                        gold=ctx.get("gold"),
                        vix=ctx.get("vix"),
                        sp_futures=ctx.get("sp_futures"),
                        regime=ctx.get("regime"),
                        source=self.name,
                        source_quality=self.source_quality,
                        event_id=event_row.id,
                    )
                )
                n_points += 1

            stats = compute_reaction_windows(path, baseline)
            db.add(
                HistoricalEventReaction(
                    event_id=event_row.id,
                    event_type=ev["event_type"],
                    surprise=surprise,
                    surprise_z=surprise_z,
                    series="USDMXN",
                    baseline_price=round(baseline, 4) if baseline else None,
                    ret_15m=stats.get("ret_15m"),
                    ret_1h=stats.get("ret_1h"),
                    ret_4h=stats.get("ret_4h"),
                    ret_1d=stats.get("ret_1d"),
                    ret_3d=stats.get("ret_3d"),
                    ret_5d=stats.get("ret_5d"),
                    max_favorable_excursion=stats.get("max_favorable_excursion"),
                    max_adverse_excursion=stats.get("max_adverse_excursion"),
                    time_to_peak_hours=stats.get("time_to_peak_hours"),
                    reversal_behavior=stats.get("reversal_behavior", "unknown"),
                    data_completeness=stats.get("data_completeness", 1.0),
                    dxy=ctx.get("dxy"),
                    us2y=ctx.get("us2y"),
                    us10y=ctx.get("us10y"),
                    oil=ctx.get("oil"),
                    gold=ctx.get("gold"),
                    vix=ctx.get("vix"),
                    sp_futures=ctx.get("sp_futures"),
                    momentum=ctx.get("momentum"),
                    regime=ctx.get("regime"),
                    news_tags=ctx.get("news_tags"),
                    source=self.name,
                    source_quality=self.source_quality,
                )
            )
            n_reactions += 1
            n_events += 1

        db.commit()
        return {
            "importer": self.name,
            "events": n_events,
            "reactions": n_reactions,
            "price_points": n_points,
        }


# --------------------------------------------------------------------------- #
# Working mock/sample importer.
# --------------------------------------------------------------------------- #
class MockSampleImporter(HistoricalImporter):
    """Self-contained historical sample; deterministic synthetic reaction paths."""

    name = "mock"
    source_quality = "sample"

    SAMPLE_EVENTS: list[dict] = [
        {
            "event_type": "us_cpi", "event_name": "US CPI (MoM)", "country": "US",
            "release_time": _dt(2023, 2, 14), "forecast": 0.4, "actual": 0.5,
            "previous": 0.1, "importance": "high", "currency_impact": "USD",
            "baseline": 18.62,
            "context": {"dxy": 103.6, "us2y": 4.62, "us10y": 3.75, "oil": 79.0,
                        "gold": 1865.0, "vix": 18.9, "sp_futures": 4150.0,
                        "momentum": 0.02, "regime": "Inflation Driven",
                        "news_tags": ["inflation", "cpi", "fed"]},
        },
        {
            "event_type": "us_nfp", "event_name": "US Nonfarm Payrolls", "country": "US",
            "release_time": _dt(2023, 5, 5), "forecast": 180.0, "actual": 253.0,
            "previous": 165.0, "importance": "high", "currency_impact": "USD",
            "baseline": 17.74,
            "context": {"dxy": 101.3, "us2y": 3.92, "us10y": 3.44, "oil": 71.3,
                        "gold": 2020.0, "vix": 17.2, "sp_futures": 4090.0,
                        "momentum": -0.03, "regime": "Trending",
                        "news_tags": ["jobs", "nfp", "labor"]},
        },
        {
            "event_type": "fed_rate_decision", "event_name": "FOMC Rate Decision",
            "country": "US", "release_time": _dt(2023, 7, 26, 18), "forecast": 5.25,
            "actual": 5.50, "previous": 5.25, "importance": "high",
            "currency_impact": "USD", "baseline": 16.74,
            "context": {"dxy": 101.0, "us2y": 4.85, "us10y": 3.87, "oil": 79.5,
                        "gold": 1972.0, "vix": 13.9, "sp_futures": 4590.0,
                        "momentum": -0.01, "regime": "Fed Driven",
                        "news_tags": ["fed", "rates", "fomc"]},
        },
        {
            "event_type": "banxico_rate_decision", "event_name": "Banxico Rate Decision",
            "country": "MX", "release_time": _dt(2023, 8, 10, 19), "forecast": 11.25,
            "actual": 11.25, "previous": 11.25, "importance": "high",
            "currency_impact": "MXN", "baseline": 17.10,
            "context": {"dxy": 102.5, "us2y": 4.80, "us10y": 4.08, "oil": 84.4,
                        "gold": 1920.0, "vix": 16.0, "sp_futures": 4480.0,
                        "momentum": 0.05, "regime": "Banxico Driven",
                        "news_tags": ["banxico", "rates", "mexico"]},
        },
        {
            "event_type": "us_cpi", "event_name": "US CPI (MoM)", "country": "US",
            "release_time": _dt(2023, 11, 14), "forecast": 0.1, "actual": 0.0,
            "previous": 0.4, "importance": "high", "currency_impact": "USD",
            "baseline": 17.46,
            "context": {"dxy": 105.7, "us2y": 5.02, "us10y": 4.63, "oil": 78.2,
                        "gold": 1945.0, "vix": 14.2, "sp_futures": 4420.0,
                        "momentum": -0.02, "regime": "Inflation Driven",
                        "news_tags": ["inflation", "cpi", "fed"]},
        },
        {
            "event_type": "mexico_cpi", "event_name": "Mexico CPI (YoY)", "country": "MX",
            "release_time": _dt(2024, 1, 9), "forecast": 4.6, "actual": 4.9,
            "previous": 4.66, "importance": "medium", "currency_impact": "MXN",
            "baseline": 17.02,
            "context": {"dxy": 102.4, "us2y": 4.35, "us10y": 4.05, "oil": 72.7,
                        "gold": 2030.0, "vix": 13.4, "sp_futures": 4760.0,
                        "momentum": 0.01, "regime": "Inflation Driven",
                        "news_tags": ["mexico", "inflation", "cpi"]},
        },
        {
            "event_type": "us_nfp", "event_name": "US Nonfarm Payrolls", "country": "US",
            "release_time": _dt(2024, 2, 2), "forecast": 185.0, "actual": 353.0,
            "previous": 216.0, "importance": "high", "currency_impact": "USD",
            "baseline": 17.16,
            "context": {"dxy": 103.9, "us2y": 4.36, "us10y": 4.02, "oil": 72.3,
                        "gold": 2040.0, "vix": 13.8, "sp_futures": 4960.0,
                        "momentum": 0.03, "regime": "Trending",
                        "news_tags": ["jobs", "nfp", "labor"]},
        },
        {
            "event_type": "fed_rate_decision", "event_name": "FOMC Rate Decision",
            "country": "US", "release_time": _dt(2024, 3, 20, 18), "forecast": 5.50,
            "actual": 5.50, "previous": 5.50, "importance": "high",
            "currency_impact": "USD", "baseline": 16.78,
            "context": {"dxy": 103.4, "us2y": 4.60, "us10y": 4.27, "oil": 81.6,
                        "gold": 2160.0, "vix": 13.0, "sp_futures": 5240.0,
                        "momentum": -0.02, "regime": "Fed Driven",
                        "news_tags": ["fed", "rates", "fomc"]},
        },
        {
            "event_type": "us_cpi", "event_name": "US CPI (MoM)", "country": "US",
            "release_time": _dt(2024, 4, 10), "forecast": 0.3, "actual": 0.4,
            "previous": 0.4, "importance": "high", "currency_impact": "USD",
            "baseline": 16.45,
            "context": {"dxy": 104.3, "us2y": 4.74, "us10y": 4.43, "oil": 85.2,
                        "gold": 2340.0, "vix": 15.0, "sp_futures": 5230.0,
                        "momentum": 0.01, "regime": "Inflation Driven",
                        "news_tags": ["inflation", "cpi", "fed"]},
        },
        {
            "event_type": "us_gdp", "event_name": "US GDP (QoQ)", "country": "US",
            "release_time": _dt(2024, 4, 25), "forecast": 2.5, "actual": 1.6,
            "previous": 3.4, "importance": "medium", "currency_impact": "USD",
            "baseline": 17.10,
            "context": {"dxy": 105.6, "us2y": 4.99, "us10y": 4.66, "oil": 83.0,
                        "gold": 2330.0, "vix": 15.4, "sp_futures": 5070.0,
                        "momentum": 0.0, "regime": "Range Bound",
                        "news_tags": ["gdp", "growth"]},
        },
        {
            "event_type": "banxico_rate_decision", "event_name": "Banxico Rate Decision",
            "country": "MX", "release_time": _dt(2024, 8, 8, 19), "forecast": 11.00,
            "actual": 10.75, "previous": 11.00, "importance": "high",
            "currency_impact": "MXN", "baseline": 19.18,
            "context": {"dxy": 103.2, "us2y": 3.99, "us10y": 3.94, "oil": 75.0,
                        "gold": 2430.0, "vix": 27.7, "sp_futures": 5230.0,
                        "momentum": 0.12, "regime": "High Volatility",
                        "news_tags": ["banxico", "rates", "mexico"]},
        },
        {
            "event_type": "us_cpi", "event_name": "US CPI (MoM)", "country": "US",
            "release_time": _dt(2024, 9, 11), "forecast": 0.2, "actual": 0.2,
            "previous": 0.2, "importance": "high", "currency_impact": "USD",
            "baseline": 19.92,
            "context": {"dxy": 101.7, "us2y": 3.64, "us10y": 3.66, "oil": 67.3,
                        "gold": 2510.0, "vix": 18.0, "sp_futures": 5550.0,
                        "momentum": 0.04, "regime": "Inflation Driven",
                        "news_tags": ["inflation", "cpi", "fed"]},
        },
        {
            "event_type": "fed_rate_decision", "event_name": "FOMC Rate Decision",
            "country": "US", "release_time": _dt(2024, 9, 18, 18), "forecast": 5.25,
            "actual": 5.00, "previous": 5.50, "importance": "high",
            "currency_impact": "USD", "baseline": 19.30,
            "context": {"dxy": 100.6, "us2y": 3.59, "us10y": 3.70, "oil": 71.0,
                        "gold": 2560.0, "vix": 17.1, "sp_futures": 5710.0,
                        "momentum": -0.05, "regime": "Fed Driven",
                        "news_tags": ["fed", "rates", "fomc"]},
        },
        {
            "event_type": "us_nfp", "event_name": "US Nonfarm Payrolls", "country": "US",
            "release_time": _dt(2024, 11, 1), "forecast": 113.0, "actual": 12.0,
            "previous": 223.0, "importance": "high", "currency_impact": "USD",
            "baseline": 20.10,
            "context": {"dxy": 104.3, "us2y": 4.21, "us10y": 4.38, "oil": 69.5,
                        "gold": 2740.0, "vix": 21.9, "sp_futures": 5760.0,
                        "momentum": 0.06, "regime": "Trending",
                        "news_tags": ["jobs", "nfp", "labor"]},
        },
        {
            "event_type": "us_cpi", "event_name": "US CPI (MoM)", "country": "US",
            "release_time": _dt(2025, 1, 15), "forecast": 0.3, "actual": 0.4,
            "previous": 0.3, "importance": "high", "currency_impact": "USD",
            "baseline": 20.72,
            "context": {"dxy": 109.0, "us2y": 4.36, "us10y": 4.79, "oil": 80.0,
                        "gold": 2700.0, "vix": 16.5, "sp_futures": 5980.0,
                        "momentum": 0.08, "regime": "Trade War",
                        "news_tags": ["inflation", "cpi", "tariff", "trade"]},
        },
        {
            "event_type": "banxico_rate_decision", "event_name": "Banxico Rate Decision",
            "country": "MX", "release_time": _dt(2025, 3, 27, 19), "forecast": 9.00,
            "actual": 9.00, "previous": 9.50, "importance": "high",
            "currency_impact": "MXN", "baseline": 20.28,
            "context": {"dxy": 104.0, "us2y": 3.97, "us10y": 4.34, "oil": 69.8,
                        "gold": 3020.0, "vix": 19.3, "sp_futures": 5780.0,
                        "momentum": -0.04, "regime": "Banxico Driven",
                        "news_tags": ["banxico", "rates", "mexico", "tariff"]},
        },
    ]

    def fetch_events(self) -> list[dict]:
        return [dict(e) for e in self.SAMPLE_EVENTS]

    def fetch_price_path(self, event: dict) -> list[tuple[float, float]]:
        """Deterministic synthetic USD/MXN path driven by the event's surprise."""
        baseline = float(event["baseline"])
        lean = _event_lean(event)
        true_dir = 1.0 if lean == "USD+" else -1.0  # USD+ -> USD/MXN up

        sigma = _SURPRISE_SIGMA.get(event["event_type"], 1.0) or 1.0
        actual, forecast = event.get("actual"), event.get("forecast")
        surprise_z = abs((actual - forecast) / sigma) if (actual is not None and forecast is not None) else 0.0
        imp = _IMPORTANCE_FACTOR.get(event.get("importance", "medium"), 0.65)
        # Total ~5d move in percent (capped), in the lean direction.
        total_move = true_dir * imp * (0.15 + 0.55 * min(2.5, surprise_z))

        # Deterministic noise seeded by the event so results are reproducible.
        seed = int(event["release_time"].timestamp()) ^ hash(event["event_type"]) & 0xFFFFFFFF
        rng = random.Random(seed)

        path: list[tuple[float, float]] = []
        for h in _PATH_OFFSETS:
            if h == 0.0:
                path.append((0.0, baseline))
                continue
            progress = (h / 120.0) ** 0.6                       # quick early move
            overshoot = 0.40 * total_move * pow(2.718281828, -((h - 36.0) / 30.0) ** 2)
            noise = (rng.random() - 0.5) * abs(total_move) * 0.30
            move_pct = total_move * progress + overshoot + noise
            path.append((h, baseline * (1.0 + move_pct / 100.0)))
        return path


# --------------------------------------------------------------------------- #
# Provider stubs (modular; implement when keys/files are available).
# --------------------------------------------------------------------------- #
class _NotConfiguredImporter(HistoricalImporter):
    """Shared stub: raises a clear error until the provider is implemented."""

    docs = ""

    def fetch_events(self) -> list[dict]:
        raise NotImplementedError(
            f"{self.name} importer is not implemented yet. {self.docs} "
            "Use the 'mock' importer for sample data."
        )

    def fetch_price_path(self, event: dict) -> list[tuple[float, float]]:
        raise NotImplementedError(f"{self.name} importer is not implemented yet.")


class CSVImporter(_NotConfiguredImporter):
    name = "csv"
    source_quality = "imported"
    docs = "Point it at local CSV files of bars + events."


class YahooFinanceImporter(_NotConfiguredImporter):
    name = "yahoo"
    source_quality = "vendor_free"
    docs = "Free daily bars via yfinance (no intraday; daily reaction windows only)."


class FREDImporter(_NotConfiguredImporter):
    name = "fred"
    source_quality = "official"
    docs = "Macro series (DXY proxy, yields) via FRED API (FRED_API_KEY)."


class AlphaVantageImporter(_NotConfiguredImporter):
    name = "alphavantage"
    source_quality = "vendor_free"
    docs = "FX + some intraday via Alpha Vantage (rate-limited free tier)."


class PolygonImporter(_NotConfiguredImporter):
    name = "polygon"
    source_quality = "vendor_paid"
    docs = "True intraday FX bars via Polygon.io (paid)."


_IMPORTERS: dict[str, type[HistoricalImporter]] = {
    "mock": MockSampleImporter,
    "sample": MockSampleImporter,
    "csv": CSVImporter,
    "yahoo": YahooFinanceImporter,
    "fred": FREDImporter,
    "alphavantage": AlphaVantageImporter,
    "polygon": PolygonImporter,
}


def get_importer(name: str = "mock") -> HistoricalImporter:
    """Return an importer instance by name (defaults to the mock/sample one)."""
    cls = _IMPORTERS.get((name or "mock").lower(), MockSampleImporter)
    return cls()
