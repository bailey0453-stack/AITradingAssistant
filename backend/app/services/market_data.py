"""Market data providers for USD/MXN and macro drivers.

Centroid/GFC FIX is the preferred USD/MXN source when a fresh executable
bid/ask is available.  The neutral market spot is the FIX midpoint; callers
that need executable BUY/SELL economics use the ask/bid directly via the FIX
provider.  The existing hourly FX provider remains the fallback.
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
    dxy: float | None = None
    us2y: float | None = None
    us10y: float | None = None
    treasury_yield: float | None = None
    oil: float | None = None
    gold: float | None = None
    sp_futures: float | None = None
    vix: float | None = None
    provider: str = "mock"
    source: str = "mock"
    timestamp: str | None = None
    drivers: dict = field(default_factory=dict)
    field_sources: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return asdict(self)


MACRO_FIELDS = ("dxy", "us2y", "us10y", "oil", "gold", "sp_futures", "vix")
_FIELD_PROVIDER = {
    "us2y": "fred", "us10y": "fred", "dxy": "alphavantage",
    "gold": "alphavantage", "oil": "alphavantage", "vix": "alphavantage",
    "sp_futures": "alphavantage",
}


class MarketDataProvider(ABC):
    source = "base"

    @abstractmethod
    def get_usdmxn(self) -> MarketData:
        raise NotImplementedError


def _inverse(usdmxn: float | None) -> float | None:
    if not usdmxn:
        return None
    return round(1.0 / usdmxn, 6)


class MockMarketDataProvider(MarketDataProvider):
    source = "mock"
    provider = "mock"
    BASE_USDMXN = 17.85
    BASE_DXY = 104.2
    BASE_US2Y = 4.70
    BASE_US10Y = 4.32
    BASE_OIL = 76.5
    BASE_GOLD = 2380.0
    BASE_SP = 5450.0
    BASE_VIX = 14.5

    def _macro(self) -> dict:
        us10y = round(self.BASE_US10Y + random.uniform(-0.08, 0.08), 3)
        return {"dxy": round(self.BASE_DXY + random.uniform(-0.6, 0.6), 2), "us2y": round(self.BASE_US2Y + random.uniform(-0.07, 0.07), 3), "us10y": us10y, "treasury_yield": us10y, "oil": round(self.BASE_OIL + random.uniform(-2.5, 2.5), 2), "gold": round(self.BASE_GOLD + random.uniform(-25, 25), 2), "sp_futures": round(self.BASE_SP + random.uniform(-40, 40), 2), "vix": round(self.BASE_VIX + random.uniform(-2.5, 4.0), 2)}

    def get_usdmxn(self) -> MarketData:
        return self._assemble(round(self.BASE_USDMXN + random.uniform(-0.25, 0.25), 4), self._macro(), self.provider, self.source)

    @classmethod
    def _assemble(cls, usdmxn: float, macro: dict, provider: str, source: str) -> MarketData:
        return MarketData(pair="USDMXN", usdmxn=usdmxn, inverse_usdmxn=_inverse(usdmxn), dxy=macro["dxy"], us2y=macro["us2y"], us10y=macro["us10y"], treasury_yield=macro["treasury_yield"], oil=macro["oil"], gold=macro["gold"], sp_futures=macro["sp_futures"], vix=macro["vix"], provider=provider, source=source, timestamp=_utcnow_iso(), drivers=cls._drivers(usdmxn, macro))

    @classmethod
    def _drivers(cls, usdmxn: float, macro: dict) -> dict:
        return {"dxy_delta": round(macro["dxy"] - cls.BASE_DXY, 3), "yield_delta": round(macro["us10y"] - cls.BASE_US10Y, 3), "us2y_delta": round(macro["us2y"] - cls.BASE_US2Y, 3), "oil_delta": round(macro["oil"] - cls.BASE_OIL, 3), "gold_delta": round(macro["gold"] - cls.BASE_GOLD, 2), "sp_delta": round(macro["sp_futures"] - cls.BASE_SP, 2), "vix_delta": round(macro["vix"] - cls.BASE_VIX, 2), "usdmxn_delta": round(usdmxn - cls.BASE_USDMXN, 4)}


class LiveMarketDataProvider(MarketDataProvider):
    source = "live"
    DEFAULT_BASE_URL = "https://openexchangerates.org/api/latest.json"

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.base_url = settings.fx_base_url or self.DEFAULT_BASE_URL
        self.timeout = settings.http_timeout_seconds

    def _fetch_usdmxn(self) -> float:
        if not self.settings.fx_api_key:
            raise RuntimeError("FX_API_KEY is not configured.")
        headers = {"Authorization": f"Token {self.settings.fx_api_key}"}
        try:
            resp = httpx.get(self.base_url, params={"symbols": "MXN"}, headers=headers, timeout=self.timeout)
            resp.raise_for_status(); data = resp.json()
        except Exception as exc:
            raise RuntimeError(f"FX request failed: {_scrub(str(exc), self.settings.fx_api_key)}") from None
        if isinstance(data, dict) and data.get("error"):
            raise RuntimeError(f"FX provider error: {data.get('description', data)}")
        rate = ((data or {}).get("rates") or {}).get("MXN")
        if rate is None:
            raise ValueError(f"USD/MXN not present in FX response: {data}")
        return float(rate)

    def get_usdmxn(self) -> MarketData:
        return MockMarketDataProvider._assemble(round(self._fetch_usdmxn(), 4), MockMarketDataProvider()._macro(), self.settings.fx_provider or "live", self.source)


def _scrub(text: str, secret: str | None) -> str:
    return text.replace(secret, "***REDACTED***") if secret and secret in text else text


def get_market_provider(settings: Settings | None = None) -> MarketDataProvider:
    settings = settings or get_settings()
    return LiveMarketDataProvider(settings) if settings.fx_live_enabled else MockMarketDataProvider()


def _mock_all(data: MarketData) -> MarketData:
    data.field_sources = {f: "mock" for f in ("usdmxn", *MACRO_FIELDS)}
    return data


def _fresh_fix_midpoint(max_age_seconds: float = 30.0) -> tuple[float, str] | None:
    """Return fresh executable FIX midpoint and timestamp, otherwise None."""
    try:
        from app.services.fix.provider import get_fix_quote
        quote = get_fix_quote()
        if not quote or quote.get("bid") is None or quote.get("ask") is None:
            return None
        updated = quote.get("updated_at")
        if not updated:
            return None
        dt = datetime.fromisoformat(str(updated).replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        if (datetime.now(timezone.utc) - dt).total_seconds() > max_age_seconds:
            return None
        return (float(quote["bid"]) + float(quote["ask"])) / 2.0, str(updated)
    except Exception as exc:
        logger.warning("Centroid FIX midpoint unavailable; using hourly FX fallback (%s).", exc)
        return None


def get_market_data(settings: Settings | None = None) -> MarketData:
    settings = settings or get_settings()
    if settings.is_mock:
        return _mock_all(MockMarketDataProvider().get_usdmxn())

    data = MockMarketDataProvider().get_usdmxn()
    field_sources: dict[str, str] = {}

    # Primary USD/MXN: executable Centroid bid/ask midpoint.  Only use a fresh
    # quote; otherwise retain the existing hourly provider as fallback.
    fix = _fresh_fix_midpoint()
    if fix:
        spot, fix_timestamp = fix
        data.usdmxn = round(spot, 5)
        data.inverse_usdmxn = _inverse(spot)
        data.provider = "centroid_fix"
        data.source = "live"
        data.timestamp = fix_timestamp
        field_sources["usdmxn"] = "live"
    elif settings.fx_api_key:
        try:
            spot = round(LiveMarketDataProvider(settings)._fetch_usdmxn(), 4)
            data.usdmxn = spot; data.inverse_usdmxn = _inverse(spot)
            data.provider = settings.fx_provider or "live"; data.source = "live"
            field_sources["usdmxn"] = "live"
        except Exception as exc:
            logger.warning("Live FX fetch failed (%s); using fallback for USD/MXN.", _scrub(str(exc), settings.fx_api_key))
            data.source = "fallback"; field_sources["usdmxn"] = "fallback"
    else:
        data.source = "fallback"; field_sources["usdmxn"] = "fallback"

    live_macro: dict[str, float] = {}
    if settings.macro_live_enabled:
        try:
            from app.services.macro_data import fetch_live_macro
            live_macro = fetch_live_macro(settings)
        except Exception as exc:
            logger.warning("Macro fetch failed wholesale (%s); using fallback.", _scrub(str(exc), settings.fred_api_key or ""))
    have_key = {"fred": bool(settings.fred_api_key), "alphavantage": bool(settings.alpha_vantage_api_key)}
    for fld in MACRO_FIELDS:
        if fld in live_macro:
            setattr(data, fld, live_macro[fld]); field_sources[fld] = "live"
        elif have_key.get(_FIELD_PROVIDER[fld]):
            field_sources[fld] = "fallback"
        else:
            field_sources[fld] = "mock"
    if "treasury_yield" in live_macro:
        data.treasury_yield = live_macro["treasury_yield"]
    macro = {"dxy": data.dxy, "us2y": data.us2y, "us10y": data.us10y, "treasury_yield": data.treasury_yield, "oil": data.oil, "gold": data.gold, "sp_futures": data.sp_futures, "vix": data.vix}
    data.drivers = MockMarketDataProvider._drivers(data.usdmxn, macro)
    data.field_sources = field_sources
    if not fix:
        data.timestamp = _utcnow_iso()
    return data
