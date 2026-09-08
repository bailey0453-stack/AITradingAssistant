"""Resilient live macro indicator providers.

Production macro inputs are fetched independently and are never replaced with
random/mock values. Provider order is deliberately diversified so one vendor or
rate limit cannot blank the whole model:

- FRED: US 2Y / 10Y Treasury yields (authoritative daily series).
- Yahoo Finance chart API (no key): primary intraday market quotes for DXY,
  WTI futures, gold futures, VIX, and S&P 500 futures.
- FRED: secondary source for WTI and VIX when Yahoo is unavailable.
- Alpha Vantage: tertiary fallback only; it is no longer the sole source for
  five macro fields and therefore cannot exhaust the model when its free-tier
  quota is reached.

Every successful real observation is stored in ``historical_market_snapshots``
so a Vercel cold start can reuse a verified value instead of immediately making
another vendor call. A durable value inside the normal cache TTL is treated as
fresh cache. If all live providers fail, a recent real observation may be used
for a bounded grace period and is explicitly marked ``stale_cache`` in metadata.
Anything older is omitted and contributes zero to production scoring.
"""

from __future__ import annotations

import logging
import time
from datetime import datetime, timezone
from typing import Callable, Optional
from urllib.parse import quote

import httpx

from app.config import Settings, get_settings
from app.services.secrets import scrub

logger = logging.getLogger(__name__)

FRED_URL = "https://api.stlouisfed.org/fred/series/observations"
ALPHA_VANTAGE_URL = "https://www.alphavantage.co/query"
YAHOO_CHART_URL = "https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"

_FRED_SERIES = {
    "us2y": "DGS2",
    "us10y": "DGS10",
    "oil": "DCOILWTICO",
    "vix": "VIXCLS",
}

_YAHOO_SYMBOLS = {
    "dxy": "DX-Y.NYB",
    "oil": "CL=F",
    "gold": "GC=F",
    "vix": "^VIX",
    "sp_futures": "ES=F",
}

_RANGES = {
    "dxy": (80.0, 130.0),
    "us2y": (0.0, 15.0),
    "us10y": (0.0, 15.0),
    "oil": (5.0, 250.0),
    "gold": (500.0, 6000.0),
    "vix": (3.0, 120.0),
    "sp_futures": (1000.0, 12000.0),
}

_PERSIST_SERIES = {
    "dxy": "MACRO_DXY",
    "us2y": "MACRO_US2Y",
    "us10y": "MACRO_US10Y",
    "oil": "MACRO_OIL",
    "gold": "MACRO_GOLD",
    "vix": "MACRO_VIX",
    "sp_futures": "MACRO_SP_FUTURES",
}

# Bounded fallback ages for verified real observations. Treasury releases are
# daily, while market-traded indicators should be much more recent.
_STALE_MAX_SECONDS = {
    "us2y": 48 * 3600,
    "us10y": 48 * 3600,
    "dxy": 6 * 3600,
    "oil": 6 * 3600,
    "gold": 6 * 3600,
    "vix": 6 * 3600,
    "sp_futures": 6 * 3600,
}

# Process cache: field -> (value, fetched_epoch, provider).
_CACHE: dict[str, tuple[float, float, str]] = {}
_LAST_META: dict[str, dict] = {}


def _in_range(field: str, value: float) -> bool:
    lo, hi = _RANGES.get(field, (float("-inf"), float("inf")))
    return lo <= value <= hi


def _cache_get(field: str, ttl: int) -> tuple[float, float, str] | None:
    if ttl <= 0:
        return None
    hit = _CACHE.get(field)
    if not hit:
        return None
    value, ts, provider = hit
    age = time.time() - ts
    if age <= ttl:
        return value, age, provider
    return None


def _cache_put(field: str, value: float, provider: str) -> None:
    _CACHE[field] = (value, time.time(), provider)


def _scrub(text: str, settings: Settings) -> str:
    return scrub(text, settings.fred_api_key, settings.alpha_vantage_api_key)


def _fred_latest(series_id: str, settings: Settings) -> float:
    if not settings.fred_api_key:
        raise RuntimeError("FRED_API_KEY is not configured.")
    params = {
        "series_id": series_id,
        "api_key": settings.fred_api_key,
        "file_type": "json",
        "sort_order": "desc",
        "limit": 8,
    }
    try:
        resp = httpx.get(FRED_URL, params=params, timeout=settings.http_timeout_seconds)
        resp.raise_for_status()
        data = resp.json()
    except Exception as exc:
        raise RuntimeError(f"FRED request failed: {_scrub(str(exc), settings)}") from None
    for obs in (data or {}).get("observations", []):
        raw = (obs.get("value") or "").strip()
        if raw and raw != ".":
            return float(raw)
    raise RuntimeError(f"FRED series {series_id} had no recent observation.")


def _yahoo_quote(symbol: str, settings: Settings) -> float:
    url = YAHOO_CHART_URL.format(symbol=quote(symbol, safe=""))
    params = {"interval": "5m", "range": "1d", "includePrePost": "true"}
    headers = {"User-Agent": "Mozilla/5.0 AITradingAssistant/1.0"}
    try:
        resp = httpx.get(url, params=params, headers=headers, timeout=settings.http_timeout_seconds)
        resp.raise_for_status()
        data = resp.json()
    except Exception as exc:
        raise RuntimeError(f"Yahoo market request failed for {symbol}: {exc}") from None
    result = (((data or {}).get("chart") or {}).get("result") or [])
    if not result:
        error = ((data or {}).get("chart") or {}).get("error")
        raise RuntimeError(f"Yahoo market quote {symbol} unavailable: {error or 'empty result'}")
    meta = result[0].get("meta") or {}
    value = meta.get("regularMarketPrice")
    if value is None:
        value = meta.get("previousClose")
    if value is None:
        raise RuntimeError(f"Yahoo market quote {symbol} had no price.")
    return float(value)


def _av_get(params: dict, settings: Settings) -> dict:
    if not settings.alpha_vantage_api_key:
        raise RuntimeError("ALPHA_VANTAGE_API_KEY is not configured.")
    params = {**params, "apikey": settings.alpha_vantage_api_key}
    try:
        resp = httpx.get(ALPHA_VANTAGE_URL, params=params, timeout=settings.http_timeout_seconds)
        resp.raise_for_status()
        data = resp.json()
    except Exception as exc:
        raise RuntimeError(f"Alpha Vantage request failed: {_scrub(str(exc), settings)}") from None
    if not isinstance(data, dict):
        raise RuntimeError("Alpha Vantage returned an unexpected payload.")
    for key in ("Note", "Information", "Error Message"):
        if data.get(key):
            raise RuntimeError(f"Alpha Vantage unavailable: {_scrub(str(data[key]), settings)}")
    return data


def _av_commodity_latest(function: str, settings: Settings) -> float:
    data = _av_get({"function": function, "interval": "daily"}, settings)
    for row in data.get("data", []):
        raw = (row.get("value") or "").strip()
        if raw and raw != ".":
            return float(raw)
    raise RuntimeError(f"Alpha Vantage {function} had no recent value.")


def _av_fx_rate(from_ccy: str, to_ccy: str, settings: Settings) -> float:
    data = _av_get({"function": "CURRENCY_EXCHANGE_RATE", "from_currency": from_ccy, "to_currency": to_ccy}, settings)
    block = data.get("Realtime Currency Exchange Rate") or {}
    rate = block.get("5. Exchange Rate")
    if rate is None:
        raise RuntimeError(f"Alpha Vantage FX {from_ccy}/{to_ccy} unavailable.")
    return float(rate)


def _av_quote(symbol: str, settings: Settings) -> float:
    data = _av_get({"function": "GLOBAL_QUOTE", "symbol": symbol}, settings)
    block = data.get("Global Quote") or {}
    price = block.get("05. price")
    if not price:
        raise RuntimeError(f"Alpha Vantage quote {symbol} unavailable.")
    return float(price)


def _persistent_get(field: str, max_age_seconds: int) -> tuple[float, float, str] | None:
    """Return latest verified durable observation if recent enough."""
    try:
        from sqlalchemy import select
        from app.database import SessionLocal
        from app.models import HistoricalMarketSnapshot

        db = SessionLocal()
        try:
            row = db.execute(
                select(HistoricalMarketSnapshot)
                .where(HistoricalMarketSnapshot.series == _PERSIST_SERIES[field])
                .order_by(HistoricalMarketSnapshot.ts.desc())
                .limit(1)
            ).scalars().first()
            if not row or row.value is None or not row.ts:
                return None
            ts = row.ts
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=timezone.utc)
            age = max(0.0, (datetime.now(timezone.utc) - ts).total_seconds())
            if age > max_age_seconds:
                return None
            value = float(row.value)
            if not _in_range(field, value):
                return None
            return value, age, row.source or "durable_cache"
        finally:
            db.close()
    except Exception as exc:
        logger.debug("Durable macro cache read failed for %s: %s", field, exc)
        return None


def _persistent_put(field: str, value: float, provider: str, quality: str) -> None:
    """Persist a verified real observation; never fail the analysis request."""
    try:
        from app.database import SessionLocal
        from app.models import HistoricalMarketSnapshot

        db = SessionLocal()
        try:
            db.add(HistoricalMarketSnapshot(
                series=_PERSIST_SERIES[field],
                ts=datetime.now(timezone.utc),
                value=float(value),
                source=provider,
                source_quality=quality,
            ))
            db.commit()
        finally:
            db.close()
    except Exception as exc:
        logger.debug("Durable macro cache write failed for %s: %s", field, exc)


def _provider_chain(field: str, settings: Settings) -> list[tuple[str, str, Callable[[], float]]]:
    """Return provider candidates in preferred order for one field."""
    if field == "us2y":
        return [("fred", "FRED DGS2", lambda: _fred_latest(_FRED_SERIES[field], settings))]
    if field == "us10y":
        return [("fred", "FRED DGS10", lambda: _fred_latest(_FRED_SERIES[field], settings))]

    yahoo = ("yahoo", f"Yahoo {_YAHOO_SYMBOLS[field]}", lambda: _yahoo_quote(_YAHOO_SYMBOLS[field], settings))
    if field == "dxy":
        return [yahoo, ("alphavantage", "Alpha Vantage DXY", lambda: _av_quote("DXY", settings))]
    if field == "oil":
        return [
            yahoo,
            ("fred", "FRED DCOILWTICO", lambda: _fred_latest(_FRED_SERIES[field], settings)),
            ("alphavantage", "Alpha Vantage WTI", lambda: _av_commodity_latest("WTI", settings)),
        ]
    if field == "gold":
        return [yahoo, ("alphavantage", "Alpha Vantage XAU/USD", lambda: _av_fx_rate("XAU", "USD", settings))]
    if field == "vix":
        return [
            yahoo,
            ("fred", "FRED VIXCLS", lambda: _fred_latest(_FRED_SERIES[field], settings)),
            ("alphavantage", "Alpha Vantage VIX", lambda: _av_quote("VIX", settings)),
        ]
    if field == "sp_futures":
        return [yahoo, ("alphavantage", "Alpha Vantage SPX", lambda: _av_quote("SPX", settings))]
    return []


def _provider_enabled(provider: str, settings: Settings) -> bool:
    if provider == "fred":
        return bool(settings.fred_api_key)
    if provider == "alphavantage":
        return bool(settings.alpha_vantage_api_key)
    return True


def fetch_live_macro(settings: Settings | None = None) -> dict[str, float]:
    """Return real macro values only, with diversified provider fallback.

    Metadata for each field is available from ``get_macro_field_meta`` and
    distinguishes ``live``, ``cached`` and ``stale_cache`` observations.
    """
    settings = settings or get_settings()
    ttl = max(0, int(settings.macro_cache_seconds))
    out: dict[str, float] = {}
    global _LAST_META
    _LAST_META = {}

    for field in _RANGES:
        # 1) Warm in-process cache.
        cached = _cache_get(field, ttl)
        if cached is not None:
            value, age, provider = cached
            out[field] = value
            _LAST_META[field] = {"status": "cached", "provider": provider, "age_seconds": round(age, 1)}
            continue

        # 2) Durable cache inside normal TTL. This is what protects Vercel cold
        # starts from multiplying vendor calls.
        durable_fresh = _persistent_get(field, ttl)
        if durable_fresh is not None:
            value, age, provider = durable_fresh
            _cache_put(field, value, provider)
            out[field] = value
            _LAST_META[field] = {"status": "cached", "provider": provider, "age_seconds": round(age, 1)}
            continue

        # 3) Live provider chain.
        errors: list[str] = []
        fetched = False
        for provider, label, fetch in _provider_chain(field, settings):
            if not _provider_enabled(provider, settings):
                continue
            try:
                value = round(float(fetch()), 4)
            except Exception as exc:
                errors.append(f"{label}: {_scrub(str(exc), settings)}")
                continue
            if not _in_range(field, value):
                errors.append(f"{label}: out of range ({value})")
                continue
            quality = "daily" if provider == "fred" else "intraday"
            _cache_put(field, value, provider)
            _persistent_put(field, value, provider, quality)
            out[field] = value
            _LAST_META[field] = {"status": "live", "provider": provider, "age_seconds": 0.0}
            fetched = True
            break

        if fetched:
            continue

        # 4) Bounded verified stale cache. It remains clearly marked and is
        # never confused with a live observation.
        stale = _persistent_get(field, _STALE_MAX_SECONDS[field])
        if stale is not None:
            value, age, provider = stale
            out[field] = value
            _LAST_META[field] = {"status": "stale_cache", "provider": provider, "age_seconds": round(age, 1)}
            logger.info("Macro %s using verified stale cache age=%.0fs provider=%s after live providers failed.", field, age, provider)
        else:
            _LAST_META[field] = {"status": "unavailable", "provider": None, "age_seconds": None}
            logger.info("Macro %s unavailable; no verified cache. %s", field, " | ".join(errors[-3:]))

    if "us10y" in out:
        out["treasury_yield"] = out["us10y"]
    return out


def get_macro_field_meta() -> dict[str, dict]:
    """Metadata for the most recent ``fetch_live_macro`` call."""
    return {k: dict(v) for k, v in _LAST_META.items()}


def clear_macro_cache() -> None:
    _CACHE.clear()
    _LAST_META.clear()
