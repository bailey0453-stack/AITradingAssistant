"""Live macro indicator providers (FRED + Alpha Vantage).

Fetches the macro drivers the analysis engine consumes, **per field**, so a
single unavailable symbol degrades only that field rather than the whole
snapshot:

  - FRED            -> US 2Y / US 10Y treasury yields (series DGS2 / DGS10).
  - Alpha Vantage   -> WTI oil, gold (XAU/USD); DXY / VIX / S&P attempted but
                       typically unavailable on the free tier and retained as
                       the existing (mock) value, with the reason logged.

``fetch_live_macro`` returns only the fields it could fetch live; the caller
(``market_data``) overlays them on the mock baseline and tags every field as
``live`` / ``fallback`` / ``mock``. No API key ever appears in a log line — keys
are sent as query params (provider requirement) and scrubbed from every error.

Results are cached process-wide for ``MACRO_CACHE_SECONDS`` to respect provider
rate limits (Alpha Vantage's free tier is ~25 requests/day).
"""

from __future__ import annotations

import logging
import time
from typing import Optional

import httpx

from app.config import Settings, get_settings
from app.services.secrets import scrub

logger = logging.getLogger(__name__)

FRED_URL = "https://api.stlouisfed.org/fred/series/observations"
ALPHA_VANTAGE_URL = "https://www.alphavantage.co/query"

# FRED series for the treasury yields we track.
_FRED_SERIES = {"us2y": "DGS2", "us10y": "DGS10"}

# Plausible ranges; a value outside its band is treated as unavailable so a
# wrong-scale symbol (e.g. an ETF proxy) never pollutes the analysis baseline.
_RANGES = {
    "dxy": (80.0, 130.0),
    "us2y": (0.0, 15.0),
    "us10y": (0.0, 15.0),
    "oil": (5.0, 250.0),
    "gold": (500.0, 6000.0),
    "vix": (3.0, 120.0),
    "sp_futures": (1000.0, 12000.0),
}

# Process-wide cache: {field: (value, fetched_epoch)}. Survives warm serverless
# invocations; harmless to lose on cold start.
_CACHE: dict[str, tuple[float, float]] = {}


def _in_range(field: str, value: float) -> bool:
    lo, hi = _RANGES.get(field, (float("-inf"), float("inf")))
    return lo <= value <= hi


def _cache_get(field: str, ttl: int) -> Optional[float]:
    if ttl <= 0:
        return None
    hit = _CACHE.get(field)
    if not hit:
        return None
    value, ts = hit
    if (time.time() - ts) <= ttl:
        return value
    return None


def _cache_put(field: str, value: float) -> None:
    _CACHE[field] = (value, time.time())


def _scrub(text: str, settings: Settings) -> str:
    return scrub(text, settings.fred_api_key, settings.alpha_vantage_api_key)


def _fred_latest(series_id: str, settings: Settings) -> float:
    """Most recent non-missing observation for a FRED series."""
    if not settings.fred_api_key:
        raise RuntimeError("FRED_API_KEY is not configured.")
    params = {
        "series_id": series_id,
        "api_key": settings.fred_api_key,  # FRED requires the key as a query param
        "file_type": "json",
        "sort_order": "desc",
        "limit": 8,
    }
    try:
        resp = httpx.get(FRED_URL, params=params, timeout=settings.http_timeout_seconds)
        resp.raise_for_status()
        data = resp.json()
    except Exception as exc:  # noqa: BLE001 - re-raise scrubbed
        raise RuntimeError(f"FRED request failed: {_scrub(str(exc), settings)}") from None

    for obs in (data or {}).get("observations", []):
        raw = (obs.get("value") or "").strip()
        if raw and raw != ".":
            return float(raw)
    raise RuntimeError(f"FRED series {series_id} had no recent observation.")


def _av_get(params: dict, settings: Settings) -> dict:
    """Call Alpha Vantage, raising (scrubbed) on transport/rate-limit errors."""
    if not settings.alpha_vantage_api_key:
        raise RuntimeError("ALPHA_VANTAGE_API_KEY is not configured.")
    params = {**params, "apikey": settings.alpha_vantage_api_key}
    try:
        resp = httpx.get(
            ALPHA_VANTAGE_URL, params=params, timeout=settings.http_timeout_seconds
        )
        resp.raise_for_status()
        data = resp.json()
    except Exception as exc:  # noqa: BLE001 - re-raise scrubbed
        raise RuntimeError(f"Alpha Vantage request failed: {_scrub(str(exc), settings)}") from None
    if not isinstance(data, dict):
        raise RuntimeError("Alpha Vantage returned an unexpected payload.")
    # Free-tier rate limit / informational responses.
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
    data = _av_get(
        {"function": "CURRENCY_EXCHANGE_RATE", "from_currency": from_ccy, "to_currency": to_ccy},
        settings,
    )
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


# field -> (callable producing a value, human label for logs)
def _fetchers(settings: Settings):
    return {
        "us2y": (lambda: _fred_latest(_FRED_SERIES["us2y"], settings), "FRED DGS2"),
        "us10y": (lambda: _fred_latest(_FRED_SERIES["us10y"], settings), "FRED DGS10"),
        "oil": (lambda: _av_commodity_latest("WTI", settings), "Alpha Vantage WTI"),
        "gold": (lambda: _av_fx_rate("XAU", "USD", settings), "Alpha Vantage XAU/USD"),
        "dxy": (lambda: _av_quote("DXY", settings), "Alpha Vantage DXY"),
        "vix": (lambda: _av_quote("VIX", settings), "Alpha Vantage VIX"),
        "sp_futures": (lambda: _av_quote("SPX", settings), "Alpha Vantage SPX"),
    }


def fetch_live_macro(settings: Settings | None = None) -> dict[str, float]:
    """Return only the macro fields that could be fetched live (per field).

    Each field is independently cached, range-checked, and—on failure—skipped
    with a scrubbed reason logged. ``us10y`` also seeds ``treasury_yield``.
    """
    settings = settings or get_settings()
    ttl = settings.macro_cache_seconds
    out: dict[str, float] = {}

    have_fred = bool(settings.fred_api_key)
    have_av = bool(settings.alpha_vantage_api_key)

    for field, (fetch, label) in _fetchers(settings).items():
        # Skip fields whose provider key is absent (keeps logs quiet + saves calls).
        if field in ("us2y", "us10y") and not have_fred:
            continue
        if field in ("oil", "gold", "dxy", "vix", "sp_futures") and not have_av:
            continue

        cached = _cache_get(field, ttl)
        if cached is not None:
            out[field] = cached
            continue

        try:
            value = round(float(fetch()), 4)
        except Exception as exc:  # noqa: BLE001 - degrade per field
            logger.info("Macro %s unavailable via %s; retaining fallback (%s).",
                        field, label, _scrub(str(exc), settings))
            continue

        if not _in_range(field, value):
            logger.info("Macro %s from %s out of range (%s); retaining fallback.",
                        field, label, value)
            continue

        _cache_put(field, value)
        out[field] = value

    if "us10y" in out:
        out["treasury_yield"] = out["us10y"]
    return out


def clear_macro_cache() -> None:
    """Clear the process-wide macro cache (used by tests)."""
    _CACHE.clear()
