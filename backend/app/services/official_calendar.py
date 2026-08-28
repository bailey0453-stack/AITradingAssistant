"""Official economic calendar providers for USD/MXN.

Combines Federal Reserve, FRED, U.S. Treasury, Banco de Mexico (Banxico), and
INEGI schedules.  Event times are only assigned where the official publisher
states a release time; we do not infer FOMC or Mexico events from loose names.
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
MEXICO_CITY = ZoneInfo("America/Mexico_City")

_FRED_RELEASE_MAP = (
    ("consumer price index", "US CPI", "US", "USD", "high", 13, 30),
    ("employment situation", "US NFP / Employment Situation", "US", "USD", "high", 13, 30),
    ("gross domestic product", "US GDP", "US", "USD", "high", 13, 30),
    ("personal income and outlays", "US PCE / Personal Income", "US", "USD", "high", 13, 30),
    ("producer price index", "US PPI", "US", "USD", "medium", 13, 30),
    ("advance monthly sales for retail", "US Retail Sales", "US", "USD", "high", 13, 30),
)

# Banco de Mexico official 2026 monetary-policy calendar. Decisions are
# explicitly published at 13:00 Mexico City time. Minutes are two weeks later.
_BANXICO_2026_DECISIONS = ((2, 5), (3, 26), (5, 7), (6, 25), (8, 6), (9, 24), (11, 5), (12, 17))
_BANXICO_2026_MINUTES = ((1, 8), (2, 19), (4, 9), (5, 21), (7, 9), (8, 20), (10, 8), (11, 19))
_BANXICO_2026_REPORTS = ((2, 26), (5, 27), (8, 26), (11, 26))

# INEGI 2026 dissemination calendar. The calendar states information is
# released at 06:00 a.m. Mexico City time.
_INEGI_2026_CPI = ((1, 8), (2, 9), (3, 9), (4, 9), (5, 7), (6, 9), (7, 9), (8, 7), (9, 9), (10, 8), (11, 9), (12, 9))
_INEGI_2026_EMPLOYMENT = ((1, 26), (2, 26), (3, 27), (4, 24), (5, 28), (6, 25), (7, 24), (8, 27), (9, 25), (10, 22), (11, 26), (12, 24))
# Estimacion Oportuna del Producto Interno Bruto Trimestral (EOPIBT).
_INEGI_2026_GDP = ((1, 30), (4, 30), (7, 30), (10, 30))


def _coverage_gaps() -> list[dict]:
    # The curated official Mexico schedules currently cover calendar year 2026.
    # Surface a maintenance warning once the year rolls rather than fabricating
    # future dates.
    if datetime.now(timezone.utc).year == 2026:
        return []
    return [{
        "event": "Mexico official calendar refresh",
        "country": "MX",
        "importance": "medium",
        "currency_impact": "MXN",
        "status": "unavailable",
        "source": "unavailable",
        "note": "Banxico/INEGI annual schedules need the new calendar year loaded.",
    }]


class _VisibleTextParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(); self.parts=[]; self._hidden_depth=0
    def handle_starttag(self, tag, attrs):  # noqa: ANN001
        if tag in {"script", "style"}: self._hidden_depth += 1
    def handle_endtag(self, tag):
        if tag in {"script", "style"} and self._hidden_depth: self._hidden_depth -= 1
    def handle_data(self, data):
        if self._hidden_depth: return
        text=" ".join(data.split())
        if text: self.parts.append(text)


def _iso(dt): return dt.astimezone(timezone.utc).isoformat()
def _event_status(release_time, now): return "released" if release_time <= now else "upcoming"
def _parse_day(day, hour=13, minute=30):
    return datetime.strptime(day[:10], "%Y-%m-%d").replace(tzinfo=timezone.utc, hour=hour, minute=minute)
def _next_month(dt):
    return dt.replace(year=dt.year+1, month=1, day=1) if dt.month == 12 else dt.replace(month=dt.month+1, day=1)


def _event(name, country, currency, importance, dt, source, note=None):
    now=datetime.now(timezone.utc)
    return {"event":name,"country":country,"release_time":_iso(dt),"forecast":None,"previous":None,"actual":None,
            "importance":importance,"currency_impact":currency,"status":_event_status(dt,now),"source":source,"note":note}


def _fed_time(value):
    m=re.match(r"^(\d{1,2}):(\d{2})\s*([ap])\.?m\.?$", value.strip(), re.I)
    if not m: return None
    h=int(m.group(1))%12
    if m.group(3).lower()=="p": h+=12
    return h,int(m.group(2))


def _fed_importance(title, details, section):
    joined=" ".join([title,*details]).lower()
    if section=="FOMC Meetings" or any(x in joined for x in ("chairman","chair ","chair-","fomc","monetary policy")): return "high"
    return "medium"


def _parse_fed_month(html, year, month, now):
    p=_VisibleTextParser(); p.feed(html); lines=p.parts; events=[]
    sections=("Speeches","Testimony","FOMC Meetings")
    stops={"Board Meetings","Beige Book","Statistical Releases","Conferences","Other","View Calendar by Month","Previous","Next"}
    i=0; active=None
    while i < len(lines):
        line=lines[i]
        if line in sections: active=line; i+=1; continue
        if line in stops: active=None; i+=1; continue
        if not active or not _fed_time(line): i+=1; continue
        hour,minute=_fed_time(line); j=i+1
        while j<len(lines) and lines[j] in {"Time:","Release Date(s):","Watch Live","Press Conference"}: j+=1
        if j>=len(lines): break
        title=lines[j]; details=[]; day=None; k=j+1
        while k<len(lines):
            c=lines[k]
            if c in sections or c in stops or _fed_time(c): break
            if re.fullmatch(r"\d{1,2}",c) and 1<=int(c)<=31: day=int(c); k+=1; break
            if c not in {"Time:","Release Date(s):","Watch Live","Press Conference"}: details.append(c)
            k+=1
        if day is None: i=max(i+1,k); continue
        try: release=datetime(year,month,day,hour,minute,tzinfo=EASTERN).astimezone(timezone.utc)
        except ValueError: i=k; continue
        detail_title=next((d for d in details if not d.lower().startswith("at ")),None)
        display=title if not detail_title else f"{title} — {detail_title}"
        events.append(_event(display,"US","USD",_fed_importance(title,details,active),release,"Federal Reserve"," ".join(details[-2:]) if details else None))
        i=k
    return events


class FederalReserveCalendarProvider:
    source="federal_reserve"
    def __init__(self,settings): self.settings=settings; self.timeout=settings.http_timeout_seconds
    def get_events(self):
        now=datetime.now(timezone.utc); current=now.astimezone(EASTERN).replace(day=1); events=[]; errors=[]
        for dt in (current,_next_month(current)):
            url=FED_CALENDAR_MONTH_URL.format(year=dt.year,month=month_name[dt.month].lower())
            try:
                r=httpx.get(url,timeout=self.timeout,headers={"User-Agent":"FX-Intelligence/1.0"},follow_redirects=True); r.raise_for_status()
                events.extend(_parse_fed_month(r.text,dt.year,dt.month,now))
            except Exception as exc: errors.append(str(exc))
        if not events: raise RuntimeError("Federal Reserve calendar returned no relevant events: "+"; ".join(errors))
        return _dedupe_events(events)


class FREDEconomicCalendarProvider:
    source="fred"
    def __init__(self,settings): self.settings=settings; self.timeout=settings.http_timeout_seconds
    def get_events(self):
        key=self.settings.fred_api_key
        if not key: raise RuntimeError("FRED_API_KEY is not configured.")
        now=datetime.now(timezone.utc)
        params={"api_key":key,"file_type":"json","realtime_start":(now-timedelta(days=7)).date().isoformat(),"realtime_end":(now+timedelta(days=45)).date().isoformat(),"include_release_dates_with_no_data":"true","limit":1000,"sort_order":"asc"}
        try: r=httpx.get(FRED_RELEASES_DATES_URL,params=params,timeout=self.timeout); r.raise_for_status(); rows=(r.json() or {}).get("release_dates") or []
        except Exception as exc: raise RuntimeError(f"FRED release calendar failed: {scrub(str(exc),key)}") from None
        events=[]; seen=set()
        for row in rows:
            name=(row.get("release_name") or "").strip(); day=(row.get("date") or "").strip()
            if not name or not day: continue
            low=name.lower(); mapped=next((x for x in _FRED_RELEASE_MAP if x[0] in low),None)
            if not mapped: continue
            _,display,country,currency,importance,hour,minute=mapped; dt=_parse_day(day,hour,minute)
            if (display,day) in seen: continue
            seen.add((display,day)); events.append(_event(display,country,currency,importance,dt,"FRED"))
        if not events: raise RuntimeError("FRED release calendar returned no USD/MXN-relevant releases.")
        return events


class TreasuryAuctionCalendarProvider:
    source="treasury"
    def __init__(self,settings): self.settings=settings; self.timeout=settings.http_timeout_seconds
    def get_events(self):
        now=datetime.now(timezone.utc); params={"filter":f"auction_date:gte:{now.date().isoformat()},auction_date:lte:{(now+timedelta(days=45)).date().isoformat()}","sort":"auction_date","page[size]":100}
        try: r=httpx.get(TREASURY_UPCOMING_AUCTIONS_URL,params=params,timeout=self.timeout); r.raise_for_status(); rows=(r.json() or {}).get("data") or []
        except Exception as exc: raise RuntimeError(f"Treasury auction calendar failed: {exc}") from None
        events=[]
        for row in rows:
            day=(row.get("auction_date") or "").strip(); typ=(row.get("security_type") or "Treasury").strip(); term=(row.get("security_term") or "").strip()
            if not day: continue
            imp="high" if typ.lower() in {"note","bond"} or "10-year" in term.lower() else "medium"
            events.append(_event(f"US Treasury {term} {typ} Auction","US","USD",imp,_parse_day(day,15,30),"U.S. Treasury"))
        if not events: raise RuntimeError("Treasury auction calendar returned no upcoming auctions.")
        return events


class BanxicoCalendarProvider:
    """Banco de Mexico official 2026 policy schedule."""
    source="banxico"
    def __init__(self,settings): self.settings=settings
    def get_events(self):
        now=datetime.now(timezone.utc)
        if now.year != 2026: raise RuntimeError("Banxico annual schedule for this year is not loaded.")
        events=[]
        for month,day in _BANXICO_2026_DECISIONS:
            dt=datetime(2026,month,day,13,0,tzinfo=MEXICO_CITY).astimezone(timezone.utc)
            events.append(_event("Banxico Monetary Policy Decision","MX","MXN","high",dt,"Banco de México","Official 2026 monetary-policy decision calendar; published at 13:00 Mexico City time."))
        for month,day in _BANXICO_2026_MINUTES:
            dt=datetime(2026,month,day,13,0,tzinfo=MEXICO_CITY).astimezone(timezone.utc)
            events.append(_event("Banxico Monetary Policy Minutes","MX","MXN","medium",dt,"Banco de México","Official 2026 policy-minutes calendar."))
        for month,day in _BANXICO_2026_REPORTS:
            dt=datetime(2026,month,day,13,0,tzinfo=MEXICO_CITY).astimezone(timezone.utc)
            events.append(_event("Banxico Quarterly Report","MX","MXN","medium",dt,"Banco de México","Official 2026 quarterly-report calendar."))
        return events


class INEGICalendarProvider:
    """INEGI official 2026 dissemination schedule for market-moving MX data."""
    source="inegi"
    def __init__(self,settings): self.settings=settings
    def get_events(self):
        now=datetime.now(timezone.utc)
        if now.year != 2026: raise RuntimeError("INEGI annual schedule for this year is not loaded.")
        events=[]
        for month,day in _INEGI_2026_CPI:
            dt=datetime(2026,month,day,6,0,tzinfo=MEXICO_CITY).astimezone(timezone.utc)
            events.append(_event("Mexico CPI (INEGI INPC)","MX","MXN","high",dt,"INEGI","Official INEGI 2026 dissemination calendar; 06:00 Mexico City time."))
        for month,day in _INEGI_2026_GDP:
            dt=datetime(2026,month,day,6,0,tzinfo=MEXICO_CITY).astimezone(timezone.utc)
            events.append(_event("Mexico GDP - Timely Estimate (INEGI)","MX","MXN","high",dt,"INEGI","Estimación Oportuna del Producto Interno Bruto Trimestral; official 2026 calendar."))
        for month,day in _INEGI_2026_EMPLOYMENT:
            dt=datetime(2026,month,day,6,0,tzinfo=MEXICO_CITY).astimezone(timezone.utc)
            events.append(_event("Mexico Employment / Unemployment (INEGI ENOE)","MX","MXN","medium",dt,"INEGI","Official monthly ENOE employment release calendar; 06:00 Mexico City time."))
        return events


class CompositeOfficialCalendarProvider:
    source="official"
    def __init__(self,settings):
        self.settings=settings
        self._providers=(FederalReserveCalendarProvider(settings),FREDEconomicCalendarProvider(settings),TreasuryAuctionCalendarProvider(settings),BanxicoCalendarProvider(settings),INEGICalendarProvider(settings))
        self._status="ERROR"; self._errors=[]; self._cache=None
    @property
    def status(self): return self._status
    @property
    def coverage_gaps(self): return _coverage_gaps()
    def get_events(self):
        if self._cache is not None: return self._cache
        events=[]; errors=[]
        for provider in self._providers:
            try: events.extend(provider.get_events())
            except Exception as exc:
                msg=scrub(str(exc),self.settings.fred_api_key); errors.append(f"{provider.source}: {msg}"); logger.warning("Official calendar sub-provider %s failed: %s",provider.source,msg)
        events=_dedupe_events(events); events.sort(key=lambda e:e.get("release_time") or ""); self._errors=errors
        self._status="LIVE" if events and not errors and not self.coverage_gaps else ("PARTIAL" if events else "ERROR")
        self._cache=events; return events
    def get_upcoming(self,limit=None):
        rows=[e for e in self.get_events() if e.get("status")=="upcoming"]; rows.sort(key=lambda e:e.get("release_time") or ""); return rows[:limit] if limit else rows
    def get_recent_released(self,limit=None):
        rows=[e for e in self.get_events() if e.get("status")=="released"]; rows.sort(key=lambda e:e.get("release_time") or "",reverse=True); return rows[:limit] if limit else rows


def _dedupe_events(events):
    seen=set(); out=[]
    for ev in events:
        key=((ev.get("event") or "").strip(),(ev.get("release_time") or "")[:10])
        if key in seen: continue
        seen.add(key); out.append(ev)
    return out
