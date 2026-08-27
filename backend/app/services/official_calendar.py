"""Free official economic calendar providers for USD/MXN.

Aggregates release schedules from government sources (FRED release calendar,
U.S. Treasury upcoming auctions). Mexico-specific feeds and Fed/FOMC scheduling
without an authoritative timestamp are explicit coverage gaps rather than
fabricated events.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

import httpx

from app.config import Settings
from app.services.secrets import scrub

logger = logging.getLogger(__name__)

FRED_RELEASES_DATES_URL = "https://api.stlouisfed.org/fred/releases/dates"
TREASURY_UPCOMING_AUCTIONS_URL = (
    "https://api.fiscaldata.treasury.gov/services/api/fiscal_service"
    "/v1/accounting/od/upcoming_auctions"
)

# substring in FRED release_name (lower) -> (display event, country, currency, importance, hour UTC, minute)
# FOMC is deliberately excluded. FRED release-name matches do not provide a
# trustworthy event timestamp and previously caused false same-day event risk.
_FRED_RELEASE_MAP: tuple[tuple[str, str, str, str, str, int, int], ...] = (
    ("consumer price index", "US CPI", "US", "USD", "high", 13, 30),
    ("employment situation", "US NFP / Employment Situation", "US", "USD", "high", 13, 30),
    ("gross domestic product", "US GDP", "US", "USD", "high", 13, 30),
    ("personal income and outlays", "US PCE / Personal Income", "US", "USD", "high", 13, 30),
    ("producer price index", "US PPI", "US", "USD", "medium", 13, 30),
    ("advance monthly sales for retail", "US Retail Sales", "US", "USD", "high", 13, 30),
    ("mexico", "Mexico Economic Release (FRED)", "MX", "MXN", "medium", 13, 0),
)

# Tracked event types with no free official forward calendar — surfaced as gaps only.
COVERAGE_GAPS: tuple[dict, ...] = (
    {
        "event": "FOMC Meeting / Rate Decision",
        "country": "US",
        "importance": "high",
        "currency_impact": "USD",
        "status": "unavailable",
        "source": "unavailable",
        "note": "Authoritative Federal Reserve FOMC calendar is not integrated; FRED names are not used to infer event timing.",
    },
    {
        "event": "Banxico Rate Decision",
        "country": "MX",
        "importance": "high",
        "currency_impact": "MXN",
        "status": "unavailable",
        "source": "unavailable",
        "note": "No free official Banxico meeting calendar API configured. See banxico.org.mx for the published schedule.",
    },
    {
        "event": "Mexico CPI (official release calendar)",
        "country": "MX", "importance": "medium", "currency_impact": "MXN",
        "status": "unavailable", "source": "unavailable",
        "note": "INEGI release calendar not integrated. FRED may list some Mexico series releases when available.",
    },
    {
        "event": "Mexico GDP (official release calendar)",
        "country": "MX", "importance": "medium", "currency_impact": "MXN",
        "status": "unavailable", "source": "unavailable",
        "note": "INEGI / Banxico forward calendar not integrated.",
    },
    {
        "event": "Mexico Employment / Unemployment",
        "country": "MX", "importance": "medium", "currency_impact": "MXN",
        "status": "unavailable", "source": "unavailable",
        "note": "No free official forward calendar API configured.",
    },
    {
        "event": "Fed Chair Speech / Fed appearances",
        "country": "US", "importance": "medium", "currency_impact": "USD",
        "status": "unavailable", "source": "unavailable",
        "note": "Federal Reserve speech calendar not integrated.",
    },
)


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat()


def _parse_day(day: str, hour: int = 13, minute: int = 30) -> datetime:
    base = datetime.strptime(day[:10], "%Y-%m-%d").replace(tzinfo=timezone.utc)
    return base.replace(hour=hour, minute=minute)


def _event_status(release_time: datetime, now: datetime) -> str:
    return "released" if release_time <= now else "upcoming"


class FREDEconomicCalendarProvider:
    source = "fred"
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.timeout = settings.http_timeout_seconds
    def get_events(self) -> list[dict]:
        key = self.settings.fred_api_key
        if not key:
            raise RuntimeError("FRED_API_KEY is not configured.")
        now = datetime.now(timezone.utc)
        params = {"api_key": key, "file_type": "json", "realtime_start": (now - timedelta(days=7)).date().isoformat(), "realtime_end": (now + timedelta(days=45)).date().isoformat(), "include_release_dates_with_no_data": "true", "limit": 1000, "sort_order": "asc"}
        try:
            resp = httpx.get(FRED_RELEASES_DATES_URL, params=params, timeout=self.timeout)
            resp.raise_for_status(); data = resp.json()
        except Exception as exc:
            raise RuntimeError(f"FRED release calendar failed: {scrub(str(exc), key)}") from None
        rows = (data or {}).get("release_dates") or []
        if not isinstance(rows, list): raise RuntimeError("FRED release calendar returned an unexpected payload.")
        events=[]; seen=set()
        for row in rows:
            if not isinstance(row, dict): continue
            name=(row.get("release_name") or "").strip(); day=(row.get("date") or "").strip()
            if not name or not day: continue
            mapped=_match_fred_release(name)
            if not mapped: continue
            display,country,currency,importance,hour,minute=mapped; release_dt=_parse_day(day,hour,minute); dedupe_key=(display,day)
            if dedupe_key in seen: continue
            seen.add(dedupe_key)
            events.append({"event":display,"country":country,"release_time":_iso(release_dt),"forecast":None,"previous":None,"actual":None,"importance":importance,"currency_impact":currency,"status":_event_status(release_dt,now),"source":"FRED"})
        if not events: raise RuntimeError("FRED release calendar returned no USD/MXN-relevant releases.")
        return events


def _match_fred_release(release_name: str):
    lower=release_name.lower()
    for needle,display,country,currency,importance,hour,minute in _FRED_RELEASE_MAP:
        if needle in lower: return display,country,currency,importance,hour,minute
    return None


class TreasuryAuctionCalendarProvider:
    source="treasury"
    def __init__(self,settings:Settings)->None: self.settings=settings; self.timeout=settings.http_timeout_seconds
    def get_events(self)->list[dict]:
        now=datetime.now(timezone.utc); start=now.date().isoformat(); end=(now+timedelta(days=45)).date().isoformat(); params={"filter":f"auction_date:gte:{start},auction_date:lte:{end}","sort":"auction_date","page[size]":100}
        try:
            resp=httpx.get(TREASURY_UPCOMING_AUCTIONS_URL,params=params,timeout=self.timeout); resp.raise_for_status(); data=resp.json()
        except Exception as exc: raise RuntimeError(f"Treasury auction calendar failed: {exc}") from None
        rows=(data or {}).get("data") or []
        if not isinstance(rows,list): raise RuntimeError("Treasury auction calendar returned an unexpected payload.")
        events=[]; seen=set()
        for row in rows:
            if not isinstance(row,dict): continue
            day=(row.get("auction_date") or "").strip(); sec_type=(row.get("security_type") or "Treasury").strip(); term=(row.get("security_term") or "").strip()
            if not day: continue
            label=f"US Treasury {term} {sec_type} Auction".strip(); dedupe_key=(label,day)
            if dedupe_key in seen: continue
            seen.add(dedupe_key); release_dt=_parse_day(day,15,30); importance="high" if sec_type.lower() in {"note","bond"} or "10-year" in term.lower() else "medium"
            events.append({"event":label,"country":"US","release_time":_iso(release_dt),"forecast":None,"previous":None,"actual":None,"importance":importance,"currency_impact":"USD","status":_event_status(release_dt,now),"source":"U.S. Treasury"})
        if not events: raise RuntimeError("Treasury auction calendar returned no upcoming auctions.")
        return events


class CompositeOfficialCalendarProvider:
    source="official"
    def __init__(self,settings:Settings)->None:
        self.settings=settings; self._providers=(FREDEconomicCalendarProvider(settings),TreasuryAuctionCalendarProvider(settings)); self._status="ERROR"; self._errors=[]; self._cache=None
    @property
    def status(self): return self._status
    @property
    def coverage_gaps(self): return [dict(g) for g in COVERAGE_GAPS]
    def get_events(self):
        if self._cache is not None: return self._cache
        events=[]; errors=[]
        for provider in self._providers:
            try: events.extend(provider.get_events())
            except Exception as exc:
                msg=scrub(str(exc),self.settings.fred_api_key); errors.append(f"{provider.source}: {msg}"); logger.warning("Official calendar sub-provider %s failed: %s",provider.source,msg)
        self._errors=errors; events=_dedupe_events(events); events.sort(key=lambda e:e.get("release_time") or "")
        self._status="LIVE" if events and not errors else ("PARTIAL" if events else "ERROR")
        if events and self._status=="LIVE" and self.coverage_gaps: self._status="PARTIAL"
        self._cache=events; return events
    def get_upcoming(self,limit=None):
        events=[e for e in self.get_events() if e.get("status")=="upcoming"]; events.sort(key=lambda e:e.get("release_time") or ""); return events[:limit] if limit else events
    def get_recent_released(self,limit=None):
        events=[e for e in self.get_events() if e.get("status")=="released"]; events.sort(key=lambda e:e.get("release_time") or "",reverse=True); return events[:limit] if limit else events


def _dedupe_events(events):
    seen=set(); out=[]
    for ev in events:
        key=((ev.get("event") or "").strip(),(ev.get("release_time") or "")[:10])
        if key in seen: continue
        seen.add(key); out.append(ev)
    return out
