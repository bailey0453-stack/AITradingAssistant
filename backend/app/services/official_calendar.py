"""Free official economic calendar providers for USD/MXN.

Aggregates release schedules from government sources: the Federal Reserve's
published monthly calendar, FRED release dates, and U.S. Treasury upcoming
auctions. Mexico-specific feeds without an authoritative timestamp remain
explicit coverage gaps rather than fabricated events.
"""

from __future__ import annotations

import logging
import re
from calendar import month_name
from datetime import datetime, timedelta, timezone
from html.parser import HTMLParser
from zoneinfo import ZoneInfo

import httpx

from app.config import Settings
from app.services.secrets import scrub

logger = logging.getLogger(__name__)

FRED_RELEASES_DATES_URL = "https://api.stlouisfed.org/fred/releases/dates"
FED_CALENDAR_MONTH_URL = "https://www.federalreserve.gov/newsevents/{year}-{month}.htm"
TREASURY_UPCOMING_AUCTIONS_URL = (
    "https://api.fiscaldata.treasury.gov/services/api/fiscal_service"
    "/v1/accounting/od/upcoming_auctions"
)
EASTERN = ZoneInfo("America/New_York")

# substring in FRED release_name (lower) -> (display event, country, currency, importance, hour UTC, minute)
# FOMC is deliberately excluded. Authoritative FOMC timing comes from the
# Federal Reserve calendar provider below.
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
)


class _VisibleTextParser(HTMLParser):
    """Tiny dependency-free HTML-to-lines helper for the Fed calendar pages."""

    def __init__(self) -> None:
        super().__init__()
        self.parts: list[str] = []
        self._hidden_depth = 0

    def handle_starttag(self, tag: str, attrs) -> None:  # noqa: ANN001
        if tag in {"script", "style"}:
            self._hidden_depth += 1

    def handle_endtag(self, tag: str) -> None:
        if tag in {"script", "style"} and self._hidden_depth:
            self._hidden_depth -= 1

    def handle_data(self, data: str) -> None:
        if self._hidden_depth:
            return
        text = " ".join(data.split())
        if text:
            self.parts.append(text)


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat()


def _parse_day(day: str, hour: int = 13, minute: int = 30) -> datetime:
    base = datetime.strptime(day[:10], "%Y-%m-%d").replace(tzinfo=timezone.utc)
    return base.replace(hour=hour, minute=minute)


def _event_status(release_time: datetime, now: datetime) -> str:
    return "released" if release_time <= now else "upcoming"


def _next_month(dt: datetime) -> datetime:
    if dt.month == 12:
        return dt.replace(year=dt.year + 1, month=1, day=1)
    return dt.replace(month=dt.month + 1, day=1)


def _fed_time(value: str) -> tuple[int, int] | None:
    match = re.match(r"^(\d{1,2}):(\d{2})\s*([ap])\.?m\.?$", value.strip(), re.I)
    if not match:
        return None
    hour = int(match.group(1)) % 12
    if match.group(3).lower() == "p":
        hour += 12
    return hour, int(match.group(2))


def _fed_importance(title: str, details: list[str], section: str) -> str:
    joined = " ".join([title, *details]).lower()
    if section == "FOMC Meetings":
        return "high"
    if any(x in joined for x in ("chairman", "chair ", "chair-", "fomc", "monetary policy")):
        return "high"
    if any(x in joined for x in ("vice chair", "governor")):
        return "medium"
    return "medium"


def _parse_fed_month(html: str, year: int, month: int, now: datetime) -> list[dict]:
    parser = _VisibleTextParser()
    parser.feed(html)
    lines = parser.parts
    events: list[dict] = []
    sections = ("Speeches", "Testimony", "FOMC Meetings")
    stop_sections = {
        "Board Meetings", "Beige Book", "Statistical Releases", "Conferences", "Other",
        "View Calendar by Month", "Previous", "Next",
    }

    i = 0
    active: str | None = None
    while i < len(lines):
        line = lines[i]
        if line in sections:
            active = line
            i += 1
            continue
        if line in stop_sections:
            active = None
            i += 1
            continue
        if not active:
            i += 1
            continue

        parsed_time = _fed_time(line)
        if not parsed_time:
            i += 1
            continue

        hour, minute = parsed_time
        j = i + 1
        while j < len(lines) and lines[j] in {"Time:", "Release Date(s):", "Watch Live", "Press Conference"}:
            j += 1
        if j >= len(lines):
            break
        title = lines[j]
        details: list[str] = []
        day: int | None = None
        k = j + 1
        while k < len(lines):
            candidate = lines[k]
            if candidate in sections or candidate in stop_sections or _fed_time(candidate):
                break
            if re.fullmatch(r"\d{1,2}", candidate):
                n = int(candidate)
                if 1 <= n <= 31:
                    day = n
                    k += 1
                    break
            if candidate not in {"Time:", "Release Date(s):", "Watch Live", "Press Conference"}:
                details.append(candidate)
            k += 1
        if day is None:
            i = max(i + 1, k)
            continue

        try:
            local_dt = datetime(year, month, day, hour, minute, tzinfo=EASTERN)
        except ValueError:
            i = k
            continue
        release_dt = local_dt.astimezone(timezone.utc)
        detail_title = next((d for d in details if not d.lower().startswith("at ")), None)
        display = title if not detail_title else f"{title} — {detail_title}"
        events.append(
            {
                "event": display,
                "country": "US",
                "release_time": _iso(release_dt),
                "forecast": None,
                "previous": None,
                "actual": None,
                "importance": _fed_importance(title, details, active),
                "currency_impact": "USD",
                "status": _event_status(release_dt, now),
                "source": "Federal Reserve",
                "note": " ".join(details[-2:]) if details else None,
            }
        )
        i = k
    return events


class FederalReserveCalendarProvider:
    """Official Fed speeches/testimony/FOMC calendar with Eastern timestamps."""

    source = "federal_reserve"

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.timeout = settings.http_timeout_seconds

    def get_events(self) -> list[dict]:
        now = datetime.now(timezone.utc)
        current_et = now.astimezone(EASTERN).replace(day=1)
        months = (current_et, _next_month(current_et))
        events: list[dict] = []
        errors: list[str] = []
        for dt in months:
            url = FED_CALENDAR_MONTH_URL.format(year=dt.year, month=month_name[dt.month].lower())
            try:
                resp = httpx.get(
                    url,
                    timeout=self.timeout,
                    headers={"User-Agent": "FX-Intelligence/1.0 (official-calendar-reader)"},
                    follow_redirects=True,
                )
                resp.raise_for_status()
                events.extend(_parse_fed_month(resp.text, dt.year, dt.month, now))
            except Exception as exc:  # noqa: BLE001
                errors.append(f"{dt.year}-{dt.month:02d}: {exc}")
        events = _dedupe_events(events)
        if not events:
            detail = "; ".join(errors) if errors else "no relevant events parsed"
            raise RuntimeError(f"Federal Reserve calendar returned no relevant events ({detail}).")
        return events


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
        self.settings=settings
        self._providers=(FederalReserveCalendarProvider(settings),FREDEconomicCalendarProvider(settings),TreasuryAuctionCalendarProvider(settings))
        self._status="ERROR"; self._errors=[]; self._cache=None
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
