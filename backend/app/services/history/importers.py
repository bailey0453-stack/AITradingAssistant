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


# Market time-series the import framework is designed to load (Phase 5). The
# mock/sample build seeds reaction paths only; real providers (CSV / Yahoo /
# FRED / Alpha Vantage / Polygon) fill these standalone series when configured.
SUPPORTED_SERIES: tuple[str, ...] = (
    "USDMXN",       # daily + hourly
    "USDMXN_1H",
    "DXY",
    "US2Y",
    "US10Y",
    "OIL",
    "GOLD",
    "VIX",
    "SP_FUTURES",
)

# Standalone series name -> the snapshot column its scalar value belongs in, so
# a US2Y bar lands in `us2y` (not `usdmxn`) and similarity context stays clean.
SERIES_COLUMN: dict[str, str] = {
    "USDMXN": "usdmxn",
    "USDMXN_1H": "usdmxn",
    "DXY": "dxy",
    "US2Y": "us2y",
    "US10Y": "us10y",
    "OIL": "oil",
    "GOLD": "gold",
    "VIX": "vix",
    "SP_FUTURES": "sp_futures",
}


class HistoricalImporter(ABC):
    """Base importer: subclasses supply data, this class persists + computes.

    Two capabilities, declared per importer so the orchestrator (`run_all`)
    knows what to call:

      - ``provides_events``  -> implements ``fetch_events`` + ``fetch_price_path``
        (populates events + reactions + event-linked snapshots via ``run``).
      - ``provides_series``  -> implements ``fetch_series`` (populates standalone
        ``historical_market_snapshots`` rows via ``run_series``).

    ``lazy_safe`` marks importers cheap enough to run on a page load when the DB
    is empty (local/no-network: mock, csv). Network importers are CLI-only.
    """

    name = "base"
    source_quality = "unknown"
    provides_events = True
    provides_series = False
    lazy_safe = False

    @abstractmethod
    def fetch_events(self) -> list[dict]:
        raise NotImplementedError

    @abstractmethod
    def fetch_price_path(self, event: dict) -> list[tuple[float, float]]:
        """Return [(hours_after_event, usdmxn_price)] including (0.0, baseline)."""
        raise NotImplementedError

    def fetch_series(self) -> list[dict]:
        """Optional: standalone market series bars to backfill.

        Each bar: ``{"series", "ts" (datetime), "value", ...optional macro}``.
        Default is empty; real providers override this. Persisted via
        :meth:`run_series` into ``historical_market_snapshots`` (no event link).
        """
        return []

    def run_series(self, db: Session) -> dict:
        """Persist standalone market series bars (best-effort, no event link)."""
        bars = self.fetch_series()
        n = 0
        for bar in bars:
            series = bar.get("series", "USDMXN")
            column = SERIES_COLUMN.get(series)
            fields = {
                "usdmxn": bar.get("usdmxn"),
                "dxy": bar.get("dxy"),
                "us2y": bar.get("us2y"),
                "us10y": bar.get("us10y"),
                "oil": bar.get("oil"),
                "gold": bar.get("gold"),
                "vix": bar.get("vix"),
                "sp_futures": bar.get("sp_futures"),
            }
            # Route a generic scalar `value` into the right column for its series.
            value = bar.get("value")
            if column and value is not None and fields.get(column) is None:
                fields[column] = value
            db.add(
                HistoricalMarketSnapshot(
                    series=series,
                    ts=bar["ts"],
                    regime=bar.get("regime"),
                    source=self.name,
                    source_quality=self.source_quality,
                    **fields,
                )
            )
            n += 1
        if n:
            db.commit()
        return {"importer": self.name, "series_points": n}

    def run_all(self, db: Session) -> dict:
        """Run every capability this importer declares; never raise.

        Returns a combined summary with per-stage errors collected (scrubbed by
        the providers themselves) so a CLI/diagnostics caller can report cleanly.
        """
        out = {
            "importer": self.name,
            "source_quality": self.source_quality,
            "events": 0,
            "reactions": 0,
            "price_points": 0,
            "series_points": 0,
            "errors": [],
        }
        if self.provides_events:
            try:
                r = self.run(db)
                out["events"] = r.get("events", 0)
                out["reactions"] = r.get("reactions", 0)
                out["price_points"] = r.get("price_points", 0)
            except Exception as exc:  # noqa: BLE001 - report, don't crash
                db.rollback()
                out["errors"].append(f"events: {exc}")
        if self.provides_series:
            try:
                r = self.run_series(db)
                out["series_points"] = r.get("series_points", 0)
            except Exception as exc:  # noqa: BLE001 - report, don't crash
                db.rollback()
                out["errors"].append(f"series: {exc}")
        return out

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
    provides_events = True
    provides_series = False
    lazy_safe = True  # no network, no key — safe to seed on demand

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

    # Building blocks for the synthetic library (deterministic expansion).
    _SYNTH_TYPES: list[tuple[str, str, str, str, float]] = [
        # (event_type, event_name, country, currency_impact, surprise_scale)
        ("us_cpi", "US CPI (MoM)", "US", "USD", 0.2),
        ("us_ppi", "US PPI (MoM)", "US", "USD", 0.3),
        ("us_nfp", "US Nonfarm Payrolls", "US", "USD", 60.0),
        ("us_gdp", "US GDP (QoQ)", "US", "USD", 0.5),
        ("fed_rate_decision", "FOMC Rate Decision", "US", "USD", 0.25),
        ("banxico_rate_decision", "Banxico Rate Decision", "MX", "MXN", 0.25),
        ("mexico_cpi", "Mexico CPI (YoY)", "MX", "MXN", 0.2),
        ("mexico_gdp", "Mexico GDP (QoQ)", "MX", "MXN", 0.4),
    ]
    _SYNTH_REGIMES: list[tuple[str, list[str]]] = [
        ("Inflation Driven", ["inflation", "cpi", "fed"]),
        ("Fed Driven", ["fed", "rates", "fomc"]),
        ("Banxico Driven", ["banxico", "rates", "mexico"]),
        ("Risk On", ["risk", "equities"]),
        ("Risk Off", ["risk", "haven"]),
        ("Trade War", ["tariff", "trade", "mexico"]),
        ("Trending", ["momentum", "trend"]),
        ("Range Bound", ["range", "consolidation"]),
        ("High Volatility", ["volatility", "vix"]),
    ]

    def _synthetic_events(self, count: int = 72) -> list[dict]:
        """Deterministically generate a broad historical library.

        Spans 2019–2025 across event types, surprise sizes, and market regimes
        so nearest-neighbor matching has a meaningful (25+) evidence base
        without any paid data provider. Tagged as sample-quality data.
        """
        rng = random.Random(20240601)
        events: list[dict] = []
        base_price = 18.5
        for i in range(count):
            etype, ename, country, impact, sigma = self._SYNTH_TYPES[i % len(self._SYNTH_TYPES)]
            regime, tags = self._SYNTH_REGIMES[i % len(self._SYNTH_REGIMES)]
            # Spread releases roughly every ~30 days from 2019-01.
            day = _dt(2019, 1, 15) + timedelta(days=int(i * 31.5) + rng.randint(0, 6))
            forecast = round(rng.uniform(0.0, 5.0), 2)
            surprise = rng.uniform(-1.8, 1.8) * sigma
            actual = round(forecast + surprise, 3)
            # Drift the baseline across the period (USD/MXN broadly 17–21).
            base_price = max(16.5, min(21.5, base_price + rng.uniform(-0.35, 0.4)))
            events.append({
                "event_type": etype,
                "event_name": ename,
                "country": country,
                "release_time": day,
                "forecast": forecast,
                "actual": actual,
                "previous": round(forecast + rng.uniform(-0.5, 0.5), 3),
                "importance": "high" if i % 3 == 0 else ("medium" if i % 3 == 1 else "low"),
                "currency_impact": impact,
                "baseline": round(base_price, 4),
                "context": {
                    "dxy": round(rng.uniform(96.0, 110.0), 2),
                    "us2y": round(rng.uniform(0.2, 5.2), 2),
                    "us10y": round(rng.uniform(0.6, 4.9), 2),
                    "oil": round(rng.uniform(40.0, 95.0), 1),
                    "gold": round(rng.uniform(1500.0, 3050.0), 1),
                    "vix": round(rng.uniform(11.0, 34.0), 1),
                    "sp_futures": round(rng.uniform(2800.0, 6000.0), 1),
                    "momentum": round(rng.uniform(-0.15, 0.15), 3),
                    "regime": regime,
                    "news_tags": tags,
                },
            })
        return events

    def fetch_events(self) -> list[dict]:
        # Curated anchor events first, then the deterministic synthetic library.
        return [dict(e) for e in self.SAMPLE_EVENTS] + self._synthetic_events()

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
    provides_events = False
    provides_series = True
    lazy_safe = False

    def fetch_events(self) -> list[dict]:
        raise NotImplementedError(
            f"{self.name} importer is not implemented yet. {self.docs} "
            "Use the 'mock' importer for sample data."
        )

    def fetch_price_path(self, event: dict) -> list[tuple[float, float]]:
        raise NotImplementedError(f"{self.name} importer is not implemented yet.")

    def fetch_series(self) -> list[dict]:
        raise NotImplementedError(
            f"{self.name} importer is not implemented yet. {self.docs} "
            "Use 'csv', 'alphavantage', or 'fred' for real backfill."
        )


class CSVImporter(HistoricalImporter):
    """Load historical events + USD/MXN reaction paths from local CSV files.

    Configure ``CSV_HISTORY_DIR`` (env) pointing at a directory containing:

      - ``events.csv`` — one row per event with columns:
        ``event_key,event_type,event_name,country,release_time,forecast,actual,
        previous,importance,currency_impact,baseline,dxy,us2y,us10y,oil,gold,
        vix,sp_futures,momentum,regime,news_tags`` (``news_tags`` pipe-separated,
        ``release_time`` ISO-8601).
      - ``paths.csv`` — reaction paths with columns ``event_key,hours,price``.
      - ``series.csv`` (optional) — standalone market time-series with columns
        ``series,ts,value`` where ``series`` is one of USDMXN, DXY, US2Y, US10Y,
        OIL, GOLD, VIX, SP_FUTURES and ``ts`` is ISO-8601. Loaded into
        ``historical_market_snapshots`` independently of events.

    This is a real, no-paid-provider loader: drop CSVs exported from any source
    (Yahoo, FRED, broker, manual) and import them. ``events.csv`` is required;
    ``paths.csv`` and ``series.csv`` are optional.
    """

    name = "csv"
    source_quality = "imported"
    provides_events = True
    provides_series = True
    lazy_safe = True  # local files only — no network, safe to seed on demand

    def __init__(self, directory: str | None = None) -> None:
        import os

        self.directory = directory or os.getenv("CSV_HISTORY_DIR")
        self._paths: dict[str, list[tuple[float, float]]] | None = None

    def _require_dir(self) -> str:
        import os

        if not self.directory:
            raise NotImplementedError(
                "CSV importer needs CSV_HISTORY_DIR set (or directory=) pointing "
                "at events.csv + paths.csv. Use the 'mock' importer for samples."
            )
        if not os.path.isdir(self.directory):
            raise FileNotFoundError(f"CSV_HISTORY_DIR not found: {self.directory}")
        return self.directory

    def _load_paths(self) -> dict[str, list[tuple[float, float]]]:
        import csv
        import os

        if self._paths is not None:
            return self._paths
        directory = self._require_dir()
        paths: dict[str, list[tuple[float, float]]] = {}
        paths_file = os.path.join(directory, "paths.csv")
        if os.path.isfile(paths_file):
            with open(paths_file, newline="", encoding="utf-8") as fh:
                for row in csv.DictReader(fh):
                    key = (row.get("event_key") or "").strip()
                    try:
                        hours = float(row["hours"])
                        price = float(row["price"])
                    except (KeyError, TypeError, ValueError):
                        continue
                    paths.setdefault(key, []).append((hours, price))
        for key in paths:
            paths[key].sort(key=lambda x: x[0])
        self._paths = paths
        return paths

    def fetch_events(self) -> list[dict]:
        import csv
        import os
        from datetime import datetime as _datetime

        directory = self._require_dir()
        events_file = os.path.join(directory, "events.csv")
        if not os.path.isfile(events_file):
            raise FileNotFoundError(f"events.csv not found in {directory}")

        def _f(row: dict, key: str) -> float | None:
            val = (row.get(key) or "").strip()
            try:
                return float(val) if val != "" else None
            except ValueError:
                return None

        events: list[dict] = []
        with open(events_file, newline="", encoding="utf-8") as fh:
            for i, row in enumerate(csv.DictReader(fh)):
                rt = (row.get("release_time") or "").strip()
                try:
                    release = _datetime.fromisoformat(rt)
                except ValueError:
                    continue
                if release.tzinfo is None:
                    release = release.replace(tzinfo=timezone.utc)
                tags = [t.strip() for t in (row.get("news_tags") or "").split("|") if t.strip()]
                events.append({
                    "event_key": (row.get("event_key") or str(i)).strip(),
                    "event_type": (row.get("event_type") or "unknown").strip(),
                    "event_name": (row.get("event_name") or "").strip(),
                    "country": (row.get("country") or "US").strip(),
                    "release_time": release,
                    "forecast": _f(row, "forecast"),
                    "actual": _f(row, "actual"),
                    "previous": _f(row, "previous"),
                    "importance": (row.get("importance") or "medium").strip(),
                    "currency_impact": (row.get("currency_impact") or "USD").strip(),
                    "baseline": _f(row, "baseline"),
                    "context": {
                        "dxy": _f(row, "dxy"), "us2y": _f(row, "us2y"),
                        "us10y": _f(row, "us10y"), "oil": _f(row, "oil"),
                        "gold": _f(row, "gold"), "vix": _f(row, "vix"),
                        "sp_futures": _f(row, "sp_futures"),
                        "momentum": _f(row, "momentum"),
                        "regime": (row.get("regime") or "").strip() or None,
                        "news_tags": tags or None,
                    },
                })
        return events

    def fetch_price_path(self, event: dict) -> list[tuple[float, float]]:
        paths = self._load_paths()
        key = str(event.get("event_key", ""))
        path = paths.get(key)
        if path:
            if path[0][0] != 0.0 and event.get("baseline"):
                path = [(0.0, float(event["baseline"]))] + path
            return path
        # No intraday path supplied: fall back to a flat baseline (daily-only).
        baseline = float(event.get("baseline") or 0.0)
        return [(0.0, baseline)] if baseline else []

    def fetch_series(self) -> list[dict]:
        """Load optional standalone market series from ``series.csv``.

        Returns an empty list when the file is absent (events-only import is a
        valid configuration), so ``run_series`` simply adds nothing.
        """
        import csv
        import os
        from datetime import datetime as _datetime

        directory = self._require_dir()
        series_file = os.path.join(directory, "series.csv")
        if not os.path.isfile(series_file):
            return []

        bars: list[dict] = []
        with open(series_file, newline="", encoding="utf-8") as fh:
            for row in csv.DictReader(fh):
                series = (row.get("series") or "USDMXN").strip().upper()
                raw_ts = (row.get("ts") or "").strip()
                raw_val = (row.get("value") or "").strip()
                if not raw_ts or raw_val == "":
                    continue
                try:
                    ts = _datetime.fromisoformat(raw_ts)
                    value = float(raw_val)
                except ValueError:
                    continue
                if ts.tzinfo is None:
                    ts = ts.replace(tzinfo=timezone.utc)
                bars.append({"series": series, "ts": ts, "value": value})
        return bars


# --------------------------------------------------------------------------- #
# Yahoo Finance — free daily OHLC via the public chart API (no API key).
# --------------------------------------------------------------------------- #
class YahooFinanceImporter(HistoricalImporter):
    """Backfill daily market history from Yahoo Finance chart API."""

    name = "yahoo"
    source_quality = "vendor_free"
    provides_events = False
    provides_series = True
    lazy_safe = False
    docs = "Free daily bars via Yahoo Finance chart API (no key)."

    URL = "https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
    # Yahoo symbol -> internal series name
    SYMBOLS = {
        "MXN=X": "USDMXN",
        "DX-Y.NYB": "DXY",
        "^GSPC": "SP500",
        "^VIX": "VIX",
        "GC=F": "GOLD",
        "CL=F": "OIL",
    }

    def __init__(self, settings=None, lookback_days: int = 3650) -> None:
        from app.config import get_settings

        self.settings = settings or get_settings()
        self.lookback_days = lookback_days

    def fetch_events(self) -> list[dict]:
        raise NotImplementedError(f"{self.name} importer is series-only.")

    def fetch_price_path(self, event: dict) -> list[tuple[float, float]]:
        raise NotImplementedError(f"{self.name} importer is series-only.")

    def _chart(self, symbol: str) -> list[dict]:
        import httpx

        period1 = int(
            (datetime.now(timezone.utc) - timedelta(days=self.lookback_days)).timestamp()
        )
        period2 = int(datetime.now(timezone.utc).timestamp())
        url = self.URL.format(symbol=symbol)
        params = {"interval": "1d", "period1": period1, "period2": period2}
        try:
            resp = httpx.get(
                url,
                params=params,
                timeout=getattr(self.settings, "http_timeout_seconds", 12.0),
                headers={"User-Agent": "AITradingAssistant/1.0"},
            )
            resp.raise_for_status()
            payload = resp.json()
        except Exception as exc:  # noqa: BLE001
            raise RuntimeError(f"Yahoo chart request failed for {symbol}: {exc}") from None

        result = (payload or {}).get("chart", {}).get("result") or []
        if not result:
            raise RuntimeError(f"Yahoo returned no chart data for {symbol}.")
        block = result[0]
        timestamps = block.get("timestamp") or []
        closes = (
            (block.get("indicators") or {}).get("quote") or [{}]
        )[0].get("close") or []
        series_name = self.SYMBOLS[symbol]
        bars: list[dict] = []
        for ts_raw, close in zip(timestamps, closes):
            if close is None:
                continue
            ts = datetime.fromtimestamp(int(ts_raw), tz=timezone.utc)
            bars.append({"series": series_name, "ts": ts, "value": float(close)})
        return bars

    def fetch_series(self) -> list[dict]:
        bars: list[dict] = []
        errors: list[str] = []
        for symbol in self.SYMBOLS:
            try:
                bars.extend(self._chart(symbol))
            except Exception as exc:  # noqa: BLE001
                logger.info("Yahoo %s unavailable: %s", symbol, exc)
                errors.append(symbol)
        if not bars and errors:
            raise RuntimeError(f"Yahoo returned no data for: {', '.join(errors)}.")
        return bars


# --------------------------------------------------------------------------- #
# FRED — official macro series (treasury yields, dollar index proxy, VIX).
# --------------------------------------------------------------------------- #
class FREDImporter(HistoricalImporter):
    """Backfill official macro series from FRED into ``historical_market_snapshots``.

    Series-only (no economic-event reactions). Needs ``FRED_API_KEY``. Each FRED
    series is one request returning its full observation history; we limit the
    lookback window to keep the import bounded. The API key is sent as a query
    param (FRED requirement) and is scrubbed from every error before it surfaces.
    """

    name = "fred"
    source_quality = "official"
    provides_events = False
    provides_series = True
    lazy_safe = False
    docs = "Macro series (yields, dollar-index proxy, VIX) via FRED API (FRED_API_KEY)."

    URL = "https://api.stlouisfed.org/fred/series/observations"
    # series_name -> FRED series id
    SERIES = {
        "US2Y": "DGS2",
        "US10Y": "DGS10",
        "DXY": "DTWEXBGS",
        "VIX": "VIXCLS",
        "OIL": "DCOILWTICO",
        "FED_FUNDS": "FEDFUNDS",
        "US_CPI": "CPIAUCSL",
        "US_PCE": "PCEPI",
        "BANXICO_RATE": "INTDSRMXM193N",
        "MEXICO_CPI": "FPCPITOTLZGMEX",
    }

    def __init__(self, settings=None, lookback_days: int = 1825) -> None:
        from app.config import get_settings

        self.settings = settings or get_settings()
        self.lookback_days = lookback_days

    def fetch_events(self) -> list[dict]:
        raise NotImplementedError(f"{self.name} importer is series-only (no events).")

    def fetch_price_path(self, event: dict) -> list[tuple[float, float]]:
        raise NotImplementedError(f"{self.name} importer is series-only (no events).")

    def _scrub(self, text: str) -> str:
        from app.services.secrets import scrub

        return scrub(text, getattr(self.settings, "fred_api_key", None))

    def _observations(self, series_id: str) -> list[tuple[datetime, float]]:
        import httpx

        key = getattr(self.settings, "fred_api_key", None)
        if not key:
            raise RuntimeError("FRED_API_KEY is not configured.")
        start = (datetime.now(timezone.utc) - timedelta(days=self.lookback_days)).date()
        params = {
            "series_id": series_id,
            "api_key": key,
            "file_type": "json",
            "observation_start": start.isoformat(),
            "sort_order": "asc",
        }
        try:
            resp = httpx.get(self.URL, params=params,
                             timeout=getattr(self.settings, "http_timeout_seconds", 8.0))
            resp.raise_for_status()
            data = resp.json()
        except Exception as exc:  # noqa: BLE001 - re-raise scrubbed
            raise RuntimeError(f"FRED request failed: {self._scrub(str(exc))}") from None

        out: list[tuple[datetime, float]] = []
        for obs in (data or {}).get("observations", []):
            raw = (obs.get("value") or "").strip()
            day = (obs.get("date") or "").strip()
            if not raw or raw == "." or not day:
                continue
            try:
                ts = datetime.fromisoformat(day).replace(tzinfo=timezone.utc)
                out.append((ts, float(raw)))
            except ValueError:
                continue
        return out

    def fetch_series(self) -> list[dict]:
        bars: list[dict] = []
        errors: list[str] = []
        for series_name, series_id in self.SERIES.items():
            try:
                for ts, value in self._observations(series_id):
                    bars.append({"series": series_name, "ts": ts, "value": value})
            except Exception as exc:  # noqa: BLE001 - degrade per series
                logger.info("FRED %s (%s) unavailable: %s",
                            series_name, series_id, self._scrub(str(exc)))
                errors.append(series_name)
        if not bars and errors:
            raise RuntimeError(
                f"FRED returned no observations for: {', '.join(errors)}."
            )
        return bars


# --------------------------------------------------------------------------- #
# Alpha Vantage — FX + commodity + equity daily history (rate-limited free tier).
# --------------------------------------------------------------------------- #
class AlphaVantageImporter(HistoricalImporter):
    """Backfill daily history from Alpha Vantage into ``historical_market_snapshots``.

    Series-only. Needs ``ALPHA_VANTAGE_API_KEY``. The free tier is ~25 req/day
    and ~5 req/min, so we make a *small fixed* number of requests (USD/MXN, WTI
    oil, an S&P proxy) and throttle between them. The key is sent as a query
    param (provider requirement) and scrubbed from every error.
    """

    name = "alphavantage"
    source_quality = "vendor_free"
    provides_events = False
    provides_series = True
    lazy_safe = False
    docs = "FX + commodity + equity daily history via Alpha Vantage (ALPHA_VANTAGE_API_KEY)."

    URL = "https://www.alphavantage.co/query"
    # Seconds between requests to respect the ~5 req/min free-tier limit.
    MIN_INTERVAL_SECONDS = 13.0

    def __init__(self, settings=None, *, throttle: bool = True, outputsize: str = "compact") -> None:
        from app.config import get_settings

        self.settings = settings or get_settings()
        self.throttle = throttle
        self.outputsize = outputsize  # "compact" (~100 pts) or "full"

    def fetch_events(self) -> list[dict]:
        raise NotImplementedError(f"{self.name} importer is series-only (no events).")

    def fetch_price_path(self, event: dict) -> list[tuple[float, float]]:
        raise NotImplementedError(f"{self.name} importer is series-only (no events).")

    def _scrub(self, text: str) -> str:
        from app.services.secrets import scrub

        return scrub(text, getattr(self.settings, "alpha_vantage_api_key", None))

    def _get(self, params: dict) -> dict:
        import httpx

        key = getattr(self.settings, "alpha_vantage_api_key", None)
        if not key:
            raise RuntimeError("ALPHA_VANTAGE_API_KEY is not configured.")
        params = {**params, "apikey": key}
        try:
            resp = httpx.get(self.URL, params=params,
                             timeout=getattr(self.settings, "http_timeout_seconds", 8.0))
            resp.raise_for_status()
            data = resp.json()
        except Exception as exc:  # noqa: BLE001 - re-raise scrubbed
            raise RuntimeError(f"Alpha Vantage request failed: {self._scrub(str(exc))}") from None
        if not isinstance(data, dict):
            raise RuntimeError("Alpha Vantage returned an unexpected payload.")
        for note_key in ("Note", "Information", "Error Message"):
            if data.get(note_key):
                raise RuntimeError(f"Alpha Vantage unavailable: {self._scrub(str(data[note_key]))}")
        return data

    def _sleep(self) -> None:
        import time

        if self.throttle:
            time.sleep(self.MIN_INTERVAL_SECONDS)

    def _fx_daily(self, from_ccy: str, to_ccy: str, series_name: str) -> list[dict]:
        data = self._get({
            "function": "FX_DAILY", "from_symbol": from_ccy,
            "to_symbol": to_ccy, "outputsize": self.outputsize,
        })
        block = data.get("Time Series FX (Daily)") or {}
        return self._bars_from_daily(block, series_name, close_key="4. close")

    def _equity_daily(self, symbol: str, series_name: str) -> list[dict]:
        data = self._get({
            "function": "TIME_SERIES_DAILY", "symbol": symbol,
            "outputsize": self.outputsize,
        })
        block = data.get("Time Series (Daily)") or {}
        return self._bars_from_daily(block, series_name, close_key="4. close")

    def _commodity_daily(self, function: str, series_name: str) -> list[dict]:
        data = self._get({"function": function, "interval": "daily"})
        bars: list[dict] = []
        for row in data.get("data", []):
            day = (row.get("date") or "").strip()
            raw = (row.get("value") or "").strip()
            if not day or not raw or raw == ".":
                continue
            try:
                ts = datetime.fromisoformat(day).replace(tzinfo=timezone.utc)
                bars.append({"series": series_name, "ts": ts, "value": float(raw)})
            except ValueError:
                continue
        return bars

    @staticmethod
    def _bars_from_daily(block: dict, series_name: str, close_key: str) -> list[dict]:
        bars: list[dict] = []
        for day, fields in (block or {}).items():
            raw = (fields.get(close_key) or "").strip() if isinstance(fields, dict) else ""
            if not raw:
                continue
            try:
                ts = datetime.fromisoformat(day).replace(tzinfo=timezone.utc)
                bars.append({"series": series_name, "ts": ts, "value": float(raw)})
            except ValueError:
                continue
        return bars

    def fetch_series(self) -> list[dict]:
        # Small, fixed request set to stay within the free-tier daily cap.
        plan = [
            ("USDMXN", lambda: self._fx_daily("USD", "MXN", "USDMXN")),
            ("OIL", lambda: self._commodity_daily("WTI", "OIL")),
            ("SP_FUTURES", lambda: self._equity_daily("SPY", "SP_FUTURES")),
        ]
        bars: list[dict] = []
        errors: list[str] = []
        for i, (series_name, fetch) in enumerate(plan):
            if i > 0:
                self._sleep()
            try:
                bars.extend(fetch())
            except Exception as exc:  # noqa: BLE001 - degrade per series
                logger.info("Alpha Vantage %s unavailable: %s",
                            series_name, self._scrub(str(exc)))
                errors.append(series_name)
        if not bars and errors:
            raise RuntimeError(
                f"Alpha Vantage returned no data for: {', '.join(errors)}."
            )
        return bars


class PolygonImporter(_NotConfiguredImporter):
    name = "polygon"
    source_quality = "vendor_paid"
    docs = "True intraday FX bars via Polygon.io (paid)."


# --------------------------------------------------------------------------- #
# FRED-derived economic events (release observations, rate changes).
# --------------------------------------------------------------------------- #
class FREDEconomicEventsImporter(HistoricalImporter):
    """Build historical events from FRED macro series observation changes."""

    name = "fred_events"
    source_quality = "official"
    provides_events = True
    provides_series = False
    lazy_safe = False

    # series_id -> (event_type, event_name, country, currency_impact, importance)
    EVENT_SERIES = {
        "CPIAUCSL": ("us_cpi", "US CPI (Index)", "US", "USD", "high"),
        "PCEPI": ("us_pce", "US PCE (Index)", "US", "USD", "high"),
        "PAYEMS": ("us_nfp", "US Nonfarm Payrolls", "US", "USD", "high"),
        "FEDFUNDS": ("fed_rate_decision", "Fed Funds Effective Rate", "US", "USD", "high"),
        "INTDSRMXM193N": ("banxico_rate_decision", "Banxico Policy Rate (proxy)", "MX", "MXN", "high"),
        "FPCPITOTLZGMEX": ("mexico_cpi", "Mexico CPI (Index)", "MX", "MXN", "medium"),
    }

    def __init__(self, settings=None, lookback_days: int = 3650) -> None:
        from app.config import get_settings

        self.settings = settings or get_settings()
        self.lookback_days = lookback_days
        self._fred = FREDImporter(settings, lookback_days=lookback_days)

    def fetch_events(self) -> list[dict]:
        events: list[dict] = []
        for series_id, meta in self.EVENT_SERIES.items():
            try:
                obs = self._fred._observations(series_id)
            except Exception as exc:  # noqa: BLE001
                logger.info("FRED events series %s skipped: %s", series_id, exc)
                continue
            etype, ename, country, impact, importance = meta
            prev_val = None
            for ts, value in obs:
                if prev_val is None:
                    prev_val = value
                    continue
                if value == prev_val:
                    continue
                forecast = prev_val
                actual = value
                events.append({
                    "event_type": etype,
                    "event_name": ename,
                    "country": country,
                    "release_time": ts.replace(hour=13, minute=30),
                    "forecast": forecast,
                    "actual": actual,
                    "previous": prev_val,
                    "importance": importance,
                    "currency_impact": impact,
                    "baseline": None,
                    "context": {},
                })
                prev_val = value
        events.extend(_POWELL_SPEECH_METADATA)
        return events

    def fetch_price_path(self, event: dict) -> list[tuple[float, float]]:
        baseline = float(event.get("baseline") or 0.0)
        return [(0.0, baseline)] if baseline else []


_POWELL_SPEECH_METADATA: list[dict] = [
    {
        "event_type": "powell_speech",
        "event_name": "Chair Powell — Jackson Hole Symposium",
        "country": "US",
        "release_time": _dt(2022, 8, 26, 14),
        "forecast": None,
        "actual": None,
        "previous": None,
        "importance": "high",
        "currency_impact": "USD",
        "baseline": None,
        "context": {"news_tags": ["fed", "powell", "speech"]},
    },
    {
        "event_type": "powell_speech",
        "event_name": "Chair Powell — FOMC Press Conference",
        "country": "US",
        "release_time": _dt(2024, 9, 18, 19),
        "forecast": None,
        "actual": None,
        "previous": None,
        "importance": "high",
        "currency_impact": "USD",
        "baseline": None,
        "context": {"news_tags": ["fed", "powell", "fomc", "speech"]},
    },
]


# --------------------------------------------------------------------------- #
# Composite research backfill — multi-provider daily research database.
# --------------------------------------------------------------------------- #
class CompositeResearchImporter(HistoricalImporter):
    """Orchestrate Yahoo + FRED + optional Alpha Vantage into research snapshots."""

    name = "research"
    source_quality = "imported"
    provides_events = True
    provides_series = True
    lazy_safe = False

    def __init__(self, settings=None, lookback_days: int = 3650) -> None:
        from app.config import get_settings

        self.settings = settings or get_settings()
        self.lookback_days = lookback_days

    def fetch_events(self) -> list[dict]:
        return FREDEconomicEventsImporter(self.settings, self.lookback_days).fetch_events()

    def fetch_price_path(self, event: dict) -> list[tuple[float, float]]:
        return []

    def fetch_series(self) -> list[dict]:
        bars: list[dict] = []
        for importer in (
            YahooFinanceImporter(self.settings, self.lookback_days),
            FREDImporter(self.settings, self.lookback_days),
        ):
            try:
                bars.extend(importer.fetch_series())
            except Exception as exc:  # noqa: BLE001
                logger.info("Composite series stage %s skipped: %s", importer.name, exc)
        if getattr(self.settings, "alpha_vantage_api_key", None):
            try:
                av = AlphaVantageImporter(self.settings, outputsize="full")
                av.throttle = True
                bars.extend(av.fetch_series())
            except Exception as exc:  # noqa: BLE001
                logger.info("Composite Alpha Vantage supplement skipped: %s", exc)
        return bars

    def run_all(self, db: Session) -> dict:
        out = {
            "importer": self.name,
            "source_quality": self.source_quality,
            "events": 0,
            "reactions": 0,
            "price_points": 0,
            "series_points": 0,
            "research_snapshots": 0,
            "errors": [],
        }
        if self.provides_series:
            try:
                r = self.run_series(db)
                out["series_points"] = r.get("series_points", 0)
            except Exception as exc:  # noqa: BLE001
                db.rollback()
                out["errors"].append(f"series: {exc}")
        if self.provides_events:
            try:
                ev = FREDEconomicEventsImporter(self.settings, self.lookback_days)
                r = ev.run(db)
                out["events"] = r.get("events", 0)
                out["reactions"] = r.get("reactions", 0)
                out["price_points"] = r.get("price_points", 0)
            except Exception as exc:  # noqa: BLE001
                db.rollback()
                out["errors"].append(f"events: {exc}")
        try:
            from app.services.history.snapshot_builder import build_research_snapshots
            from datetime import date as _date

            min_date = _date.today().replace(year=_date.today().year - 10)
            built = build_research_snapshots(
                db, min_date=min_date, replace=False, source=self.name, source_quality=self.source_quality,
            )
            out["research_snapshots"] = built.get("snapshots", 0)
            out["research_range"] = {
                "start": built.get("start_date"),
                "end": built.get("end_date"),
            }
        except Exception as exc:  # noqa: BLE001
            db.rollback()
            out["errors"].append(f"snapshots: {exc}")
        return out


_IMPORTERS: dict[str, type[HistoricalImporter]] = {
    "mock": MockSampleImporter,
    "sample": MockSampleImporter,
    "csv": CSVImporter,
    "yahoo": YahooFinanceImporter,
    "fred": FREDImporter,
    "fred_events": FREDEconomicEventsImporter,
    "alphavantage": AlphaVantageImporter,
    "research": CompositeResearchImporter,
    "composite": CompositeResearchImporter,
    "polygon": PolygonImporter,
}


def get_importer(name: str = "mock", settings=None) -> HistoricalImporter:
    """Return an importer instance by name (defaults to the mock/sample one)."""
    from app.config import get_settings

    settings = settings or get_settings()
    key = (name or "mock").lower()
    cls = _IMPORTERS.get(key, MockSampleImporter)
    lookback = getattr(settings, "history_lookback_days", 3650)
    if key in ("fred", "yahoo", "fred_events", "research", "composite"):
        return cls(settings, lookback_days=lookback)
    if key == "alphavantage":
        return AlphaVantageImporter(settings)
    if key == "csv":
        return CSVImporter()
    return cls()
