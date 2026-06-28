"""USD/MXN market data endpoints + market-intelligence orchestration."""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Query
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import Settings, get_settings
from app.database import get_db
from app.models import HistoricalMarketSnapshot, MarketSnapshot, NewsItem
from app.services import cache_manager
from app.services.calendar import get_calendar_provider
from app.services.market_data import (
    MACRO_FIELDS,
    MarketData,
    MockMarketDataProvider,
    _inverse,
    get_market_data,
)
from app.services.market_hours import MarketCalendar, get_market_state, parse_holidays
from app.services.news import get_news_provider
from app.services.secrets import scrub

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/market", tags=["market"])


def store_news_items(db: Session, items: list[dict], provider: str = "mock") -> int:
    """Insert news items, skipping ones already stored (headline + published_at)."""
    stored = 0
    for item in items or []:
        headline = item.get("headline")
        if not headline:
            continue
        published_at = item.get("published_at")
        exists = db.execute(
            select(NewsItem.id)
            .where(NewsItem.headline == headline)
            .where(NewsItem.published_at == published_at)
            .limit(1)
        ).first()
        if exists:
            continue
        db.add(
            NewsItem(
                headline=headline,
                summary=item.get("summary", ""),
                source=item.get("source", ""),
                url=item.get("url", ""),
                published_at=published_at,
                sentiment=item.get("sentiment", "neutral"),
                affected_currencies=item.get("affected_currencies"),
                importance=item.get("importance", "low"),
                relevance_score=item.get("relevance_score"),
                tags=item.get("tags"),
                provider=provider,
            )
        )
        stored += 1
    return stored


def _market_calendar(settings: Settings) -> MarketCalendar:
    return MarketCalendar(holidays=parse_holidays(settings.market_holidays))


def _scrub_secrets(text: str, settings: Settings) -> str:
    """Redact every known provider key from a string before it is logged."""
    return scrub(
        text,
        settings.fx_api_key,
        getattr(settings, "fred_api_key", None),
        getattr(settings, "alpha_vantage_api_key", None),
        getattr(settings, "news_api_key", None),
        getattr(settings, "calendar_api_key", None),
    )


def _latest_snapshot(db: Session) -> MarketSnapshot | None:
    return db.execute(
        select(MarketSnapshot)
        .where(MarketSnapshot.pair == "USDMXN")
        .order_by(MarketSnapshot.created_at.desc())
        .limit(1)
    ).scalars().first()


def _latest_real_snapshot(db: Session) -> MarketSnapshot | None:
    """Latest snapshot sourced from a real *live* provider quote (never mock/fallback).

    This is the only kind of cache that may stand in for current market data in
    production — mock/fallback rows are never persisted in production, but this
    guard keeps stale demo/legacy rows from masquerading as real quotes.
    """
    return db.execute(
        select(MarketSnapshot)
        .where(MarketSnapshot.pair == "USDMXN")
        .where(MarketSnapshot.source == "live")
        .order_by(MarketSnapshot.created_at.desc())
        .limit(1)
    ).scalars().first()


def _max_age_seconds(settings: Settings) -> float:
    return max(0.0, float(getattr(settings, "market_max_age_minutes", 180)) * 60.0)


def _parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _cache_is_usable(
    row: MarketSnapshot | None, state, max_age_secs: float, *, mock_mode: bool
) -> bool:
    """Freshness rules for serving a cached quote as current market data.

    - Mock mode: any cached row is fine (local/demo).
    - Production: only a real ``live`` row, and:
        * market open  -> age must be within ``max_age_secs``;
        * market closed -> the row must be from the most recent session (no
          older than ``max_age_secs`` before the last market close), so a
          months-old quote is never shown as "last close".
    """
    if row is None:
        return False
    if mock_mode:
        return True
    if (row.source or "") != "live":
        return False
    age = _age_seconds(row)
    if age is None:
        return False
    if state.is_open:
        return age <= max_age_secs
    last_close = _parse_iso(getattr(state, "last_market_close", None))
    if last_close is None:
        return age <= max_age_secs
    created = row.created_at
    if created is not None and created.tzinfo is None:
        created = created.replace(tzinfo=timezone.utc)
    if created is None:
        return False
    from datetime import timedelta

    return created >= (last_close - timedelta(seconds=max_age_secs))


def _age_seconds(row) -> float | None:
    if row is None or row.created_at is None:
        return None
    created = row.created_at
    if created.tzinfo is None:
        created = created.replace(tzinfo=timezone.utc)
    return max(0.0, (datetime.now(timezone.utc) - created).total_seconds())


def market_data_from_snapshot(row: MarketSnapshot) -> MarketData:
    """Reconstruct a MarketData object from a stored snapshot (cache hit)."""
    macro = {
        "dxy": row.dxy, "us2y": row.us2y, "us10y": row.us10y,
        "treasury_yield": row.treasury_yield, "oil": row.oil,
        "gold": row.gold, "sp_futures": row.sp_futures, "vix": row.vix,
    }
    try:
        # Only meaningful when the full macro set is present (a real snapshot).
        drivers = (
            MockMarketDataProvider._drivers(row.usdmxn, macro)
            if row.usdmxn is not None and all(v is not None for v in macro.values())
            else {}
        )
    except (TypeError, ValueError):
        drivers = {}
    return MarketData(
        pair=row.pair,
        usdmxn=row.usdmxn,
        inverse_usdmxn=row.inverse_usdmxn if row.inverse_usdmxn else _inverse(row.usdmxn),
        dxy=row.dxy, us2y=row.us2y, us10y=row.us10y,
        treasury_yield=row.treasury_yield, oil=row.oil, gold=row.gold,
        sp_futures=row.sp_futures, vix=row.vix,
        provider=row.provider, source=row.source,
        timestamp=row.created_at.isoformat() if row.created_at else None,
        drivers=drivers,
        field_sources=row.sources or {},
    )


def _recent_news_age_seconds(db: Session) -> float | None:
    row = db.execute(
        select(NewsItem).order_by(NewsItem.created_at.desc()).limit(1)
    ).scalars().first()
    return _age_seconds(row)


def _fetch_news_if_due(db: Session, settings: Settings) -> tuple[list[dict], str]:
    """Fetch news only when the news policy is due; else reuse recent DB news."""
    age = _recent_news_age_seconds(db)
    if not cache_manager.should_refresh(
        "news", market_open=True, age_seconds=age, settings=settings
    ):
        rows = db.execute(
            select(NewsItem).order_by(NewsItem.created_at.desc()).limit(8)
        ).scalars().all()
        if rows:
            cache_manager.report_health("news", cache_manager.ProviderHealth.USING_CACHE,
                                        "within refresh interval")
            return [
                {
                    "headline": r.headline, "summary": r.summary, "source": r.source,
                    "url": r.url, "published_at": r.published_at,
                    "sentiment": r.sentiment,
                    "affected_currencies": r.affected_currencies,
                    "importance": r.importance, "relevance_score": r.relevance_score,
                    "tags": r.tags,
                }
                for r in rows
            ], "cached"
    provider = get_news_provider(settings)
    news = provider.get_news()
    news_source = getattr(provider, "source", "mock")
    store_news_items(db, news, provider=news_source)
    cache_manager.report_health("news", _news_health(news_source), f"source={news_source}")
    return news, news_source


def _news_health(source: str) -> str:
    if source == "live":
        return cache_manager.ProviderHealth.HEALTHY
    if source == "fallback":
        return cache_manager.ProviderHealth.USING_FALLBACK
    return cache_manager.ProviderHealth.HEALTHY  # mock is "healthy" mock data


def _report_market_health(market: MarketData, *, cached: bool) -> None:
    """Translate a MarketData result into per-provider health records."""
    if cached:
        cache_manager.report_health("fx", cache_manager.ProviderHealth.USING_CACHE,
                                    "market closed or within interval")
    elif market.source == "live":
        cache_manager.report_health("fx", cache_manager.ProviderHealth.HEALTHY, "live")
    elif market.source == "fallback":
        cache_manager.report_health("fx", cache_manager.ProviderHealth.USING_FALLBACK,
                                    "FX unavailable")
    else:
        cache_manager.report_health("fx", cache_manager.ProviderHealth.USING_FALLBACK, "mock")

    fs = market.field_sources or {}
    fred_fields = [f for f in ("us2y", "us10y") if fs.get(f)]
    av_fields = [f for f in ("dxy", "gold", "oil", "vix", "sp_futures") if fs.get(f)]
    if not cached:
        if any(fs.get(f) == "live" for f in fred_fields):
            cache_manager.report_health("fred", cache_manager.ProviderHealth.HEALTHY, "live yields")
        elif any(fs.get(f) == "fallback" for f in fred_fields):
            cache_manager.report_health("fred", cache_manager.ProviderHealth.USING_FALLBACK,
                                        "yields unavailable")
        if any(fs.get(f) == "live" for f in av_fields):
            cache_manager.report_health("alphavantage", cache_manager.ProviderHealth.HEALTHY,
                                        "live commodities")
        elif any(fs.get(f) == "fallback" for f in av_fields):
            cache_manager.report_health("alphavantage", cache_manager.ProviderHealth.USING_FALLBACK,
                                        "DXY/VIX/S&P unavailable on free tier")
    else:
        cache_manager.report_health("fred", cache_manager.ProviderHealth.USING_CACHE, "cached")
        cache_manager.report_health("alphavantage", cache_manager.ProviderHealth.USING_CACHE, "cached")


def _record_historical(db: Session, market: MarketData, market_status: str) -> None:
    """Auto-capture a historical time-series point on each successful LIVE refresh.

    Builds the foundation for future similarity analysis without a second process.
    """
    try:
        db.add(HistoricalMarketSnapshot(
            series="USDMXN",
            ts=datetime.now(timezone.utc),
            usdmxn=market.usdmxn, dxy=market.dxy, us2y=market.us2y, us10y=market.us10y,
            oil=market.oil, gold=market.gold, vix=market.vix, sp_futures=market.sp_futures,
            regime=None,
            source=market.provider or "live",
            source_quality="live",
        ))
    except Exception:  # noqa: BLE001 - never break the request over history capture
        logger.exception("Automatic historical capture failed; continuing.")


def _store_snapshot(
    db: Session, market: MarketData, news: list[dict], calendar: list[dict]
) -> MarketSnapshot:
    snapshot = MarketSnapshot(
        pair=market.pair,
        usdmxn=market.usdmxn,
        inverse_usdmxn=market.inverse_usdmxn,
        dxy=market.dxy,
        us2y=market.us2y,
        us10y=market.us10y,
        treasury_yield=market.treasury_yield,
        oil=market.oil,
        gold=market.gold,
        sp_futures=market.sp_futures,
        vix=market.vix,
        news=news,
        economic_calendar=calendar,
        provider=market.provider,
        source=market.source,
        sources=market.field_sources or None,
    )
    db.add(snapshot)
    db.commit()
    db.refresh(snapshot)
    return snapshot


def _build_meta(
    state, snapshot: MarketSnapshot, market: MarketData, *, cached: bool, settings: Settings
) -> dict:
    refresh_secs = cache_manager.get_refresh_seconds("usdmxn", settings)
    age_secs = _age_seconds(snapshot)
    age_min = None if age_secs is None else round(age_secs / 60, 2)
    fetched_at = snapshot.created_at.isoformat() if snapshot.created_at else None
    is_stale = bool(age_secs is not None and age_secs >= refresh_secs)
    return {
        "market_status": state.market_status,
        "market_reason": state.market_reason,
        "is_open": state.is_open,
        "provider": market.provider,
        "source": market.source,
        "cached": cached,
        "fetched_at": fetched_at,
        "cached_at": fetched_at if cached else None,
        "age_minutes": age_min,
        "refresh_interval_seconds": refresh_secs,
        "refresh_interval_minutes": round(refresh_secs / 60, 2),
        "next_refresh": state.next_expected_refresh,
        "last_market_close": state.last_market_close,
        "next_market_open": state.next_market_open,
        "is_stale": is_stale,
        "market_data_unavailable": False,
    }


def _unavailable_marketdata() -> MarketData:
    """A market result with no usable quote — never a fabricated/stale number."""
    return MarketData(
        pair="USDMXN",
        usdmxn=None,
        inverse_usdmxn=None,
        provider="unavailable",
        source="unavailable",
        timestamp=datetime.now(timezone.utc).isoformat(),
        field_sources={f: "unavailable" for f in ("usdmxn", *MACRO_FIELDS)},
    )


_UNAVAILABLE_WARNING = (
    "Live market data unavailable and no recent cached real quote exists."
)


def _build_unavailable_meta(state, settings: Settings, latest_real: MarketSnapshot | None) -> dict:
    refresh_secs = cache_manager.get_refresh_seconds("usdmxn", settings)
    last_real_at = (
        latest_real.created_at.isoformat()
        if latest_real and latest_real.created_at else None
    )
    last_real_age_min = None
    if latest_real is not None:
        age = _age_seconds(latest_real)
        last_real_age_min = None if age is None else round(age / 60, 2)
    return {
        "market_status": state.market_status,
        "market_reason": state.market_reason,
        "is_open": state.is_open,
        "provider": "unavailable",
        "source": "unavailable",
        "cached": False,
        "fetched_at": None,
        "cached_at": None,
        "age_minutes": None,
        "refresh_interval_seconds": refresh_secs,
        "refresh_interval_minutes": round(refresh_secs / 60, 2),
        "next_refresh": state.next_expected_refresh,
        "last_market_close": state.last_market_close,
        "next_market_open": state.next_market_open,
        "is_stale": True,
        "market_data_unavailable": True,
        "last_real_quote_at": last_real_at,
        "last_real_quote_age_minutes": last_real_age_min,
        "warning": _UNAVAILABLE_WARNING,
    }


def get_market_intelligence(db: Session, settings: Settings | None = None) -> dict:
    """Resolve the current market view honoring market hours + refresh policies.

    Stale-fallback safety (production, ``USE_MOCK_DATA=false``):

      1. Serve a fresh **live** quote when due and the fetch succeeds.
      2. Else serve the latest **real cached** quote if it is fresh enough
         (market open: within ``market_max_age_minutes``; closed: the most
         recent session's quote).
      3. Else return ``market_data_unavailable`` — never a hardcoded/stale rate.

    Hardcoded mock rates are used only in mock mode (``USE_MOCK_DATA=true``) for
    local development / demos, and mock/fallback quotes are never persisted as
    real snapshots in production. A successful live refresh auto-captures a
    historical snapshot.
    """
    settings = settings or get_settings()
    mock_mode = settings.is_mock
    refresh_secs = cache_manager.get_refresh_seconds("usdmxn", settings)
    max_age_secs = _max_age_seconds(settings)
    state = get_market_state(
        calendar=_market_calendar(settings), refresh_seconds=refresh_secs
    )

    latest = _latest_snapshot(db)
    latest_age = _age_seconds(latest)
    do_fetch = cache_manager.should_refresh(
        "usdmxn", market_open=state.is_open, age_seconds=latest_age, settings=settings
    )

    news: list[dict] = []
    news_source = "cached"

    # 1) Fresh fetch when due. In production only a real *live* result is
    #    accepted; a fallback (mock-valued) result is rejected so it is never
    #    stored or shown as current data.
    if do_fetch:
        try:
            market = get_market_data(settings)
        except Exception as exc:  # noqa: BLE001 - degrade to cache/unavailable
            logger.warning(
                "Market fetch failed (%s); serving cache/unavailable.",
                _scrub_secrets(str(exc), settings),
            )
            market = None
        if market is not None and (mock_mode or market.source == "live"):
            news, news_source = _fetch_news_if_due(db, settings)
            calendar = _safe_calendar(settings)
            snapshot = _store_snapshot(db, market, news, calendar)
            _report_market_health(market, cached=False)
            if market.source == "live":
                _record_historical(db, market, state.market_status)
                db.commit()
            return {
                "snapshot": snapshot, "market": market, "news": news,
                "news_source": news_source,
                "meta": _build_meta(state, snapshot, market, cached=False, settings=settings),
                "state": state,
            }
        # Production fetch failed/unavailable -> fall through to cache/unavailable.

    # 2) Serve cache when usable. Production requires a fresh *real* quote.
    cache_row = latest if mock_mode else _latest_real_snapshot(db)
    if _cache_is_usable(cache_row, state, max_age_secs, mock_mode=mock_mode):
        market = market_data_from_snapshot(cache_row)
        news, news_source = _fetch_news_if_due(db, settings)
        _report_market_health(market, cached=True)
        meta = _build_meta(state, cache_row, market, cached=True, settings=settings)
        return {
            "snapshot": cache_row, "market": market,
            "news": news or (cache_row.news or []),
            "news_source": news_source, "meta": meta, "state": state,
        }

    # 3) Mock mode with no usable cache -> seed mock (local/demo only).
    if mock_mode:
        market = get_market_data(settings)
        news, news_source = _fetch_news_if_due(db, settings)
        calendar = _safe_calendar(settings)
        snapshot = _store_snapshot(db, market, news, calendar)
        _report_market_health(market, cached=False)
        return {
            "snapshot": snapshot, "market": market, "news": news,
            "news_source": news_source,
            "meta": _build_meta(state, snapshot, market, cached=False, settings=settings),
            "state": state,
        }

    # 4) Production: live unavailable and no recent real quote -> UNAVAILABLE.
    #    Never fabricate or persist a stale rate. News/calendar may still load.
    cache_manager.report_health(
        "fx", cache_manager.ProviderHealth.OFFLINE,
        "live unavailable; no recent real quote",
    )
    news, news_source = _fetch_news_if_due(db, settings)
    market = _unavailable_marketdata()
    meta = _build_unavailable_meta(state, settings, _latest_real_snapshot(db))
    return {
        "snapshot": None, "market": market, "news": news,
        "news_source": news_source, "meta": meta, "state": state,
    }


def _safe_calendar(settings: Settings) -> list[dict]:
    try:
        provider = get_calendar_provider(settings)
        events = provider.get_upcoming(limit=6)
        cache_manager.report_health("calendar", _cal_health(getattr(provider, "source", "mock")),
                                    f"source={getattr(provider, 'source', 'mock')}")
        return events
    except Exception:  # noqa: BLE001
        cache_manager.report_health("calendar", cache_manager.ProviderHealth.OFFLINE, "error")
        return []


def _cal_health(source: str) -> str:
    if source in ("live", "imported"):
        return cache_manager.ProviderHealth.HEALTHY
    if source == "fallback":
        return cache_manager.ProviderHealth.USING_FALLBACK
    return cache_manager.ProviderHealth.HEALTHY


def capture_market_snapshot(
    db: Session,
) -> tuple[MarketSnapshot, MarketData, list[dict], str, dict]:
    """Resolve the market view (hours- + policy-aware) and persist as needed.

    Returns ``(snapshot, market, news, news_source, meta)``. Backwards-compatible
    callers can ignore ``meta``. Market hours and refresh policies are honored:
    USD/MXN is never requested while the market is closed.
    """
    intel = get_market_intelligence(db, get_settings())
    return (
        intel["snapshot"], intel["market"], intel["news"],
        intel["news_source"], intel["meta"],
    )


def serialize_market(snapshot: MarketSnapshot) -> dict:
    return {
        "id": snapshot.id,
        "pair": snapshot.pair,
        "created_at": snapshot.created_at.isoformat() if snapshot.created_at else None,
        "usdmxn": snapshot.usdmxn,
        "inverse_usdmxn": snapshot.inverse_usdmxn,
        "dxy": snapshot.dxy,
        "us2y": snapshot.us2y,
        "us10y": snapshot.us10y,
        "treasury_yield": snapshot.treasury_yield,
        "oil": snapshot.oil,
        "gold": snapshot.gold,
        "sp_futures": snapshot.sp_futures,
        "vix": snapshot.vix,
        "news": snapshot.news,
        "economic_calendar": snapshot.economic_calendar,
        "provider": snapshot.provider,
        "source": snapshot.source,
        "sources": snapshot.sources or {},
    }


@router.get("/usdmxn")
def get_usdmxn(db: Session = Depends(get_db)) -> dict:
    """Return the current USD/MXN view with market-state + cache metadata.

    Honors market hours and refresh policies: while the FX market is closed the
    latest stored session is served and no live request is made.
    """
    intel = get_market_intelligence(db, get_settings())
    snapshot = intel["snapshot"]
    if snapshot is None:
        # Market data unavailable: never emit a fabricated/stale quote.
        market = intel["market"]
        payload = {
            "pair": "USDMXN",
            "usdmxn": None,
            "inverse_usdmxn": None,
            "dxy": None, "us2y": None, "us10y": None, "treasury_yield": None,
            "oil": None, "gold": None, "sp_futures": None, "vix": None,
            "news": intel.get("news") or [],
            "economic_calendar": [],
            "provider": "unavailable",
            "source": "unavailable",
            "sources": market.field_sources or {},
        }
        payload.update(intel["meta"])
        return payload
    payload = serialize_market(snapshot)
    payload.update(intel["meta"])
    return payload


@router.get("/status")
def market_status(db: Session = Depends(get_db)) -> dict:
    """Current market hours state without forcing a data fetch."""
    settings = get_settings()
    refresh_secs = cache_manager.get_refresh_seconds("usdmxn", settings)
    state = get_market_state(
        calendar=_market_calendar(settings), refresh_seconds=refresh_secs
    )
    latest = _latest_snapshot(db)
    return {
        **state.to_dict(),
        "refresh_interval_minutes": round(refresh_secs / 60, 2),
        "last_snapshot_at": latest.created_at.isoformat() if latest and latest.created_at else None,
        "policies": cache_manager.policies_view(settings),
        "provider_health": cache_manager.health_snapshot(),
    }


@router.get("/usdmxn/history")
def get_usdmxn_history(
    limit: int = Query(default=20, ge=1, le=200),
    db: Session = Depends(get_db),
) -> dict:
    """Return the most recent stored USD/MXN snapshots."""
    rows = db.execute(
        select(MarketSnapshot)
        .where(MarketSnapshot.pair == "USDMXN")
        .order_by(MarketSnapshot.created_at.desc())
        .limit(limit)
    ).scalars().all()
    return {"count": len(rows), "snapshots": [serialize_market(r) for r in rows]}
