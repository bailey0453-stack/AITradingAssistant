"""Market data providers for USD/MXN and macro drivers.

`get_market_data()` is the single entrypoint used by the routers. It is
resilient by design:

  - USE_MOCK_DATA=true            -> MockMarketDataProvider, source="mock"
  - live desired + fetch ok       -> LiveMarketDataProvider,  source="live"
  - live desired + key missing    -> mock data,               source="fallback"
  - live desired + fetch fails     -> mock data,               source="fallback"

Only the USD/MXN spot price is fetched live in Phase 1. DXY, US 10Y yield and
oil remain placeholders (mocked) until dedicated macro providers are added, so
the analysis engine always has drivers to work with.
"""

from __future__ import annotations

import logging
import random
from abc import ABC, abstractmethod
from dataclasses import asdict, dataclass, field

import httpx

from app.config import Settings, get_settings

logger = logging.getLogger(__name__)


@dataclass
class MarketData:
    pair: str = "USDMXN"
    usdmxn: float | None = None
    dxy: float | None = None  # US Dollar Index (placeholder)
    treasury_yield: float | None = None  # US 10Y yield % (placeholder)
    oil: float | None = None  # WTI crude USD/bbl (placeholder)
    source: str = "mock"  # one of: mock | live | fallback
    drivers: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return asdict(self)


class MarketDataProvider(ABC):
    source = "base"

    @abstractmethod
    def get_usdmxn(self) -> MarketData:
        """Return a current USD/MXN snapshot with macro drivers."""
        raise NotImplementedError


class MockMarketDataProvider(MarketDataProvider):
    """Deterministic-ish but lively mocked data, good enough to build against."""

    source = "mock"

    # Rough, realistic baselines around which we jitter.
    BASE_USDMXN = 17.85
    BASE_DXY = 104.2
    BASE_YIELD = 4.32
    BASE_OIL = 76.5

    def _macro(self) -> dict:
        """Mocked macro placeholders shared by mock + live providers."""
        dxy = round(self.BASE_DXY + random.uniform(-0.6, 0.6), 2)
        treasury_yield = round(self.BASE_YIELD + random.uniform(-0.08, 0.08), 3)
        oil = round(self.BASE_OIL + random.uniform(-2.5, 2.5), 2)
        return {"dxy": dxy, "treasury_yield": treasury_yield, "oil": oil}

    def get_usdmxn(self) -> MarketData:
        usdmxn = round(self.BASE_USDMXN + random.uniform(-0.25, 0.25), 4)
        macro = self._macro()
        return MarketData(
            pair="USDMXN",
            usdmxn=usdmxn,
            dxy=macro["dxy"],
            treasury_yield=macro["treasury_yield"],
            oil=macro["oil"],
            source=self.source,
            drivers=self._drivers(usdmxn, macro),
        )

    @classmethod
    def _drivers(cls, usdmxn: float, macro: dict) -> dict:
        # Deltas vs baseline give the analysis engine something to chew on.
        return {
            "dxy_delta": round(macro["dxy"] - cls.BASE_DXY, 3),
            "yield_delta": round(macro["treasury_yield"] - cls.BASE_YIELD, 3),
            "oil_delta": round(macro["oil"] - cls.BASE_OIL, 3),
            "usdmxn_delta": round(usdmxn - cls.BASE_USDMXN, 4),
        }


class LiveMarketDataProvider(MarketDataProvider):
    """Fetches a real USD/MXN spot price from an FX API.

    Default provider: Open Exchange Rates (USD-based `latest.json`). USD/MXN is
    simply `rates["MXN"]` because rates are quoted per USD. Macro indicators
    remain mocked placeholders in Phase 1.

    Raises on any failure so the orchestrator can fall back to mock data.
    """

    source = "live"
    DEFAULT_BASE_URL = "https://openexchangerates.org/api/latest.json"

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.base_url = settings.fx_base_url or self.DEFAULT_BASE_URL
        self.timeout = settings.http_timeout_seconds

    def _fetch_usdmxn(self) -> float:
        if not self.settings.fx_api_key:
            raise RuntimeError("FX_API_KEY is not configured.")

        params = {"app_id": self.settings.fx_api_key, "symbols": "MXN"}
        resp = httpx.get(self.base_url, params=params, timeout=self.timeout)
        resp.raise_for_status()
        data = resp.json()

        # Some providers wrap errors in a 200 body.
        if isinstance(data, dict) and data.get("error"):
            raise RuntimeError(f"FX provider error: {data.get('description', data)}")

        rates = (data or {}).get("rates") or {}
        rate = rates.get("MXN")
        if rate is None:
            raise ValueError(f"USD/MXN not present in FX response: {data}")
        return float(rate)

    def get_usdmxn(self) -> MarketData:
        usdmxn = round(self._fetch_usdmxn(), 4)
        # Macro indicators remain placeholders for now.
        macro = MockMarketDataProvider()._macro()
        return MarketData(
            pair="USDMXN",
            usdmxn=usdmxn,
            dxy=macro["dxy"],
            treasury_yield=macro["treasury_yield"],
            oil=macro["oil"],
            source=self.source,
            drivers=MockMarketDataProvider._drivers(usdmxn, macro),
        )


def _fallback_data() -> MarketData:
    data = MockMarketDataProvider().get_usdmxn()
    data.source = "fallback"
    return data


def get_market_provider(settings: Settings | None = None) -> MarketDataProvider:
    """Return the preferred provider (live when enabled, else mock).

    Note: prefer `get_market_data()` for request handling — it adds the
    fallback behavior and correct source tagging.
    """
    settings = settings or get_settings()
    if settings.fx_live_enabled:
        return LiveMarketDataProvider(settings)
    return MockMarketDataProvider()


def get_market_data(settings: Settings | None = None) -> MarketData:
    """Resilient USD/MXN fetch with mock fallback and clear source tagging."""
    settings = settings or get_settings()

    # Explicit mock mode.
    if settings.is_mock:
        return MockMarketDataProvider().get_usdmxn()

    # Live desired but no key configured -> fallback.
    if not settings.fx_api_key:
        logger.warning("Live FX requested but FX_API_KEY missing; using fallback.")
        return _fallback_data()

    # Attempt live fetch; fall back on any error.
    try:
        return LiveMarketDataProvider(settings).get_usdmxn()
    except Exception as exc:  # noqa: BLE001 - any failure should degrade gracefully
        logger.warning("Live FX fetch failed (%s); using fallback.", exc)
        return _fallback_data()
