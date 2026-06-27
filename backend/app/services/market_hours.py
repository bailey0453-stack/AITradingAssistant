"""Market-hours service for the global USD/MXN FX market.

The spot FX market trades continuously from **Sunday 21:00 UTC** (Sydney open,
~5pm ET) through **Friday 21:00 UTC** (New York close), then closes for the
weekend. This service answers "should we even be requesting new prices right
now?" so providers never burn API calls when the market is shut.

It returns a :class:`MarketState` describing:

- ``market_status``       — OPEN | CLOSED | WEEKEND | HOLIDAY | EARLY_CLOSE | MAINTENANCE
- ``market_reason``       — human-readable explanation
- ``is_open``             — convenience bool
- ``last_market_close``   — ISO 8601 of the most recent close boundary
- ``next_market_open``    — ISO 8601 of the next open boundary
- ``next_expected_refresh`` — when the next live refresh should occur (when open,
  ``now + refresh_seconds``; when closed, ``next_market_open``)

The holiday / early-close / maintenance pieces are **frameworks**: pass a
``MarketCalendar`` (or rely on the env-configured default) to extend them
without code changes. Nothing is hardcoded to "weekends only".
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, timezone
from typing import Optional


# --- Status constants -------------------------------------------------------
class MarketStatus:
    OPEN = "OPEN"
    CLOSED = "CLOSED"
    WEEKEND = "WEEKEND"
    HOLIDAY = "HOLIDAY"
    EARLY_CLOSE = "EARLY_CLOSE"
    MAINTENANCE = "MAINTENANCE"


# Weekly boundaries in UTC. Python weekday(): Mon=0 .. Sun=6.
_SUNDAY = 6
_FRIDAY = 4
_WEEK_OPEN_HOUR = 21   # Sunday 21:00 UTC
_WEEK_CLOSE_HOUR = 21  # Friday 21:00 UTC


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat()


@dataclass
class MarketCalendar:
    """Extensible calendar for holidays, early closes, and maintenance windows.

    - ``holidays``: ``{date: reason}`` — full-day closures.
    - ``early_closes``: ``{date: (close_time_utc, reason)}`` — market shuts early.
    - ``maintenance``: list of ``(start_utc, end_utc, reason)`` windows.

    Defaults are empty; production can seed these from config or a data file.
    """

    holidays: dict[date, str] = field(default_factory=dict)
    early_closes: dict[date, tuple[time, str]] = field(default_factory=dict)
    maintenance: list[tuple[datetime, datetime, str]] = field(default_factory=list)

    def holiday_reason(self, d: date) -> Optional[str]:
        return self.holidays.get(d)

    def early_close(self, d: date) -> Optional[tuple[time, str]]:
        return self.early_closes.get(d)

    def maintenance_window(self, dt: datetime) -> Optional[str]:
        for start, end, reason in self.maintenance:
            if start <= dt <= end:
                return reason
        return None


@dataclass
class MarketState:
    market_status: str
    market_reason: str
    is_open: bool
    last_market_close: Optional[str]
    next_market_open: Optional[str]
    next_expected_refresh: Optional[str]

    def to_dict(self) -> dict:
        return {
            "market_status": self.market_status,
            "market_reason": self.market_reason,
            "is_open": self.is_open,
            "last_market_close": self.last_market_close,
            "next_market_open": self.next_market_open,
            "next_expected_refresh": self.next_expected_refresh,
        }


def _within_trading_week(dt: datetime) -> bool:
    """True if dt falls inside the continuous Sun 21:00 → Fri 21:00 UTC window."""
    wd = dt.weekday()
    if wd == _SUNDAY:
        return dt.hour >= _WEEK_OPEN_HOUR
    if wd == _FRIDAY:
        return dt.hour < _WEEK_CLOSE_HOUR
    if wd == 5:  # Saturday
        return False
    return True  # Mon–Thu: open all day


def _prev_friday_close(dt: datetime) -> datetime:
    """Most recent Friday 21:00 UTC at or before dt."""
    days_since_fri = (dt.weekday() - _FRIDAY) % 7
    candidate = (dt - timedelta(days=days_since_fri)).replace(
        hour=_WEEK_CLOSE_HOUR, minute=0, second=0, microsecond=0
    )
    if candidate > dt:
        candidate -= timedelta(days=7)
    return candidate


def _next_sunday_open(dt: datetime, calendar: MarketCalendar) -> datetime:
    """Next Sunday 21:00 UTC strictly after dt, skipping holiday open days."""
    days_until_sun = (_SUNDAY - dt.weekday()) % 7
    candidate = (dt + timedelta(days=days_until_sun)).replace(
        hour=_WEEK_OPEN_HOUR, minute=0, second=0, microsecond=0
    )
    if candidate <= dt:
        candidate += timedelta(days=7)
    # Skip a Sunday that is itself a holiday.
    for _ in range(8):
        if calendar.holiday_reason(candidate.date()):
            candidate += timedelta(days=7)
        else:
            break
    return candidate


def get_market_state(
    now: Optional[datetime] = None,
    calendar: Optional[MarketCalendar] = None,
    refresh_seconds: int = 3600,
) -> MarketState:
    """Determine the current FX market state for USD/MXN."""
    now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    calendar = calendar or MarketCalendar()

    last_close = _iso(_prev_friday_close(now))
    next_open = _iso(_next_sunday_open(now, calendar))

    def closed(status: str, reason: str) -> MarketState:
        return MarketState(
            market_status=status,
            market_reason=reason,
            is_open=False,
            last_market_close=last_close,
            next_market_open=next_open,
            next_expected_refresh=next_open,
        )

    # 1. Maintenance windows take precedence (framework; default none).
    mreason = calendar.maintenance_window(now)
    if mreason:
        return closed(MarketStatus.MAINTENANCE, mreason)

    # 2. Full-day holiday (framework; default none).
    hreason = calendar.holiday_reason(now.date())
    if hreason:
        return closed(MarketStatus.HOLIDAY, f"Market holiday: {hreason}.")

    # 3. Weekend.
    if not _within_trading_week(now):
        return closed(
            MarketStatus.WEEKEND,
            "FX market is closed for the weekend (Fri 21:00–Sun 21:00 UTC).",
        )

    # 4. Early close (framework; default none).
    ec = calendar.early_close(now.date())
    if ec and now.timetz().replace(tzinfo=None) >= ec[0]:
        return closed(MarketStatus.EARLY_CLOSE, f"Early close: {ec[1]}.")

    # 5. Open.
    return MarketState(
        market_status=MarketStatus.OPEN,
        market_reason="FX market is open (continuous Sun 21:00–Fri 21:00 UTC).",
        is_open=True,
        last_market_close=last_close,
        next_market_open=next_open,
        next_expected_refresh=_iso(now + timedelta(seconds=refresh_seconds)),
    )


def parse_holidays(values) -> dict[date, str]:
    """Build a holiday map from config.

    Accepts a list of ISO date strings, or a ``{iso_date: reason}`` dict.
    Invalid entries are skipped so a bad config never breaks startup.
    """
    out: dict[date, str] = {}
    if not values:
        return out
    items = values.items() if isinstance(values, dict) else [(v, "Holiday") for v in values]
    for raw, reason in items:
        try:
            out[date.fromisoformat(str(raw))] = str(reason)
        except (TypeError, ValueError):
            continue
    return out
