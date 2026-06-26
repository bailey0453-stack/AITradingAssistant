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
from datetime import datetime, timezone

import httpx

from app.config import Settings, get_settings

logger = logging.getLogger(__name__)


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class MarketData:
    pair: str = "USDMXN"
    usdmxn: float | None = None
    inverse_usdmxn: float | None = None
    dxy: float | None = None  # US Dollar Index (placeholder)
    us2y: float | None = None  # US 2Y yield % (placeholder)
    us10y: float | None = None  # US 10Y yield % (placeholder)
    treasury_yield: float | None = None  # legacy alias of us10y
    oil: float | None = None  # WTI crude USD/bbl (placeholder)
    gold: float | None = None  # USD/oz (placeholder)
    sp_futures: float | None = None  # S&P 500 futures (placeholder)
    vix: float | None = None  # volatility index (placeholder)
    provider: str = "mock"  # provider name, e.g. openexchangerates / mock
    source: str = "mock"  # one of: mock | live | fallback
    timestamp: str | None = None  # ISO 8601
    drivers: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return asdict(self)


class MarketDataProvider(ABC):
    source = "base"

    @abstractmethod
    def get_usdmxn(self) -> MarketData:
        """Return a current USD/MXN snapshot with macro drivers."""
        raise NotImplementedError


def _inverse(usdmxn: float | None) -> float | None:
    if not usdmxn:
        return None
    return round(1.0 / usdmxn, 6)


class MockMarketDataProvider(MarketDataProvider):
    """Deterministic-ish but lively mocked data, good enough to build against."""

    source = "mock"
    provider = "mock"

    # Rough, realistic baselines around which we jitter.
    BASE_USDMXN = 17.85
    BASE_DXY = 104.2
    BASE_US2Y = 4.70
    BASE_US10Y = 4.32
    BASE_OIL = 76.5
    BASE_GOLD = 2380.0
    BASE_SP = 5450.0
    BASE_VIX = 14.5

    def _macro(self) -> dict:
        """Mocked macro placeholders shared by mock + live providers."""
        us10y = round(self.BASE_US10Y + random.uniform(-0.08, 0.08), 3)
        return {
            "dxy": round(self.BASE_DXY + random.uniform(-0.6, 0.6), 2),
            "us2y": round(self.BASE_US2Y + random.uniform(-0.07, 0.07), 3),
            "us10y": us10y,
            "treasury_yield": us10y,  # legacy alias
            "oil": round(self.BASE_OIL + random.uniform(-2.5, 2.5), 2),
            "gold": round(self.BASE_GOLD + random.uniform(-25, 25), 2),
            "sp_futures": round(self.BASE_SP + random.uniform(-40, 40), 2),
            "vix": round(self.BASE_VIX + random.uniform(-2.5, 4.0), 2),
        }

    def get_usdmxn(self) -> MarketData:
        usdmxn = round(self.BASE_USDMXN + random.uniform(-0.25, 0.25), 4)
        macro = self._macro()
        return self._assemble(usdmxn, macro, provider=self.provider, source=self.source)

    @classmethod
    def _assemble(cls, usdmxn: float, macro: dict, provider: str, source: str) -> MarketData:
        return MarketData(
            pair="USDMXN",
            usdmxn=usdmxn,
            inverse_usdmxn=_inverse(usdmxn),
            dxy=macro["dxy"],
            us2y=macro["us2y"],
            us10y=macro["us10y"],
            treasury_yield=macro["treasury_yield"],
            oil=macro["oil"],
            gold=macro["gold"],
            sp_futures=macro["sp_futures"],
            vix=macro["vix"],
            provider=provider,
            source=source,
            timestamp=_utcnow_iso(),
            drivers=cls._drivers(usdmxn, macro),
        )

    @classmethod
    def _drivers(cls, usdmxn: float, macro: dict) -> dict:
        # Deltas vs baseline give the analysis engine something to chew on.
        return {
            "dxy_delta": round(macro["dxy"] - cls.BASE_DXY, 3),
            "yield_delta": round(macro["us10y"] - cls.BASE_US10Y, 3),
            "us2y_delta": round(macro["us2y"] - cls.BASE_US2Y, 3),
            "oil_delta": round(macro["oil"] - cls.BASE_OIL, 3),
            "gold_delta": round(macro["gold"] - cls.BASE_GOLD, 2),
            "sp_delta": round(macro["sp_futures"] - cls.BASE_SP, 2),
            "vix_delta": round(macro["vix"] - cls.BASE_VIX, 2),
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

        # Send the key in the Authorization header (Open Exchange Rates supports
        # `Token <app_id>`) so it never appears in the request URL or in any
        # httpx error message / log line.
        headers = {"Authorization": f"Token {self.settings.fx_api_key}"}
        params = {"symbols": "MXN"}
        try:
            resp = httpx.get(
                self.base_url, params=params, headers=headers, timeout=self.timeout
            )
            resp.raise_for_status()
            data = resp.json()
        except Exception as exc:  # noqa: BLE001 - re-raise scrubbed
            raise RuntimeError(
                f"FX request failed: {_scrub(str(exc), self.settings.fx_api_key)}"
            ) from None

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
        # Macro indicators remain mocked placeholders for now.
        macro = MockMarketDataProvider()._macro()
        provider = self.settings.fx_provider or "live"
        return MockMarketDataProvider._assemble(
            usdmxn, macro, provider=provider, source=self.source
        )


def _scrub(text: str, secret: str | None) -> str:
    """Remove the API key from any string before it is logged."""
    if secret and secret in text:
        return text.replace(secret, "***REDACTED***")
    return text


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
        logger.warning(
            "Live FX fetch failed (%s); using fallback.",
            _scrub(str(exc), settings.fx_api_key),
        )
        return _fallback_data()
