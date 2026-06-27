"""Intelligent cache + refresh-policy + provider-health layer.

Goals:

- Minimize API usage: never refetch within a provider's refresh interval, and
  never request market-hours-gated data (USD/MXN) while the market is closed.
- Always serve *something*: prefer fresh live data, then the latest valid cache,
  and only fall back to mock when no cache exists at all.
- Track provider health so the dashboard can show what's live vs degraded.

The value cache is an in-process TTL store (survives warm serverless
invocations). Durable "last valid market snapshot" lives in the database and is
handled by the market router; this module decides *whether* a refresh is due.

Refresh policies (minutes) are configurable via the ``REFRESH_POLICIES`` env
JSON (e.g. ``{"usdmxn": 30, "news": 10}``); unknown keys are ignored.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Callable, Optional

# --- Refresh policies -------------------------------------------------------
# Default cadence per provider key, in SECONDS. ``market_gated`` keys are only
# refreshed while the FX market is open.
DEFAULT_REFRESH_POLICIES: dict[str, int] = {
    "usdmxn": 60 * 60,   # 60 min, market hours only
    "news": 5 * 60,      # 5 min
    "calendar": 30 * 60,  # 30 min
    "treasury": 15 * 60,  # 15 min (US 2Y / 10Y)
    "dxy": 15 * 60,
    "gold": 15 * 60,
    "oil": 15 * 60,
    "vix": 15 * 60,
}

# Keys whose refresh is suppressed while the market is closed.
MARKET_GATED_KEYS = {"usdmxn", "treasury", "dxy", "gold", "oil", "vix"}


def get_refresh_seconds(key: str, settings=None) -> int:
    """Refresh interval (seconds) for a provider key, honoring config overrides."""
    seconds = DEFAULT_REFRESH_POLICIES.get(key, 15 * 60)
    overrides = getattr(settings, "refresh_policies", None) if settings else None
    if overrides and key in overrides:
        try:
            # Config overrides are expressed in MINUTES for human friendliness.
            seconds = int(float(overrides[key]) * 60)
        except (TypeError, ValueError):
            pass
    return max(0, seconds)


def policies_view(settings=None) -> dict[str, dict]:
    """All policies as ``{key: {seconds, minutes, market_gated}}`` for display."""
    keys = set(DEFAULT_REFRESH_POLICIES) | set(
        getattr(settings, "refresh_policies", None) or {}
    )
    out: dict[str, dict] = {}
    for key in sorted(keys):
        secs = get_refresh_seconds(key, settings)
        out[key] = {
            "seconds": secs,
            "minutes": round(secs / 60, 2),
            "market_gated": key in MARKET_GATED_KEYS,
        }
    return out


# --- Value cache ------------------------------------------------------------
@dataclass
class CacheEntry:
    value: object
    fetched_epoch: float
    provider: str = ""
    source: str = ""
    market_status: str = ""

    def age_seconds(self, now: Optional[float] = None) -> float:
        return max(0.0, (now if now is not None else time.time()) - self.fetched_epoch)

    @property
    def fetched_at(self) -> str:
        return datetime.fromtimestamp(self.fetched_epoch, tz=timezone.utc).isoformat()


_CACHE: dict[str, CacheEntry] = {}


def get(key: str) -> Optional[CacheEntry]:
    return _CACHE.get(key)


def store(key: str, value, provider: str = "", source: str = "",
          market_status: str = "") -> CacheEntry:
    entry = CacheEntry(
        value=value, fetched_epoch=time.time(), provider=provider,
        source=source, market_status=market_status,
    )
    _CACHE[key] = entry
    return entry


def age_minutes(key: str) -> Optional[float]:
    entry = _CACHE.get(key)
    return None if entry is None else round(entry.age_seconds() / 60, 2)


def is_expired(key: str, settings=None, now: Optional[float] = None) -> bool:
    entry = _CACHE.get(key)
    if entry is None:
        return True
    return entry.age_seconds(now) >= get_refresh_seconds(key, settings)


def should_refresh(
    key: str, *, market_open: bool, age_seconds: Optional[float], settings=None
) -> bool:
    """Core decision: refresh only when due AND (if gated) the market is open.

    ``age_seconds`` is the age of the latest *durable* value (e.g. the most
    recent stored snapshot), or ``None`` when nothing exists yet.
    """
    if key in MARKET_GATED_KEYS and not market_open:
        return False  # never request gated data while the market is closed
    if age_seconds is None:
        return True   # nothing cached -> must fetch (subject to market gate)
    return age_seconds >= get_refresh_seconds(key, settings)


def clear() -> None:
    _CACHE.clear()


# --- Provider health --------------------------------------------------------
class ProviderHealth:
    HEALTHY = "healthy"
    RATE_LIMITED = "rate_limited"
    OFFLINE = "offline"
    USING_CACHE = "using_cache"
    USING_FALLBACK = "using_fallback"


@dataclass
class HealthRecord:
    provider: str
    status: str
    detail: str = ""
    updated_epoch: float = field(default_factory=time.time)

    def to_dict(self) -> dict:
        return {
            "provider": self.provider,
            "status": self.status,
            "detail": self.detail,
            "updated_at": datetime.fromtimestamp(
                self.updated_epoch, tz=timezone.utc
            ).isoformat(),
        }


_HEALTH: dict[str, HealthRecord] = {}


def report_health(provider: str, status: str, detail: str = "") -> None:
    _HEALTH[provider] = HealthRecord(provider=provider, status=status, detail=detail)


def health_snapshot() -> dict[str, dict]:
    return {name: rec.to_dict() for name, rec in sorted(_HEALTH.items())}


def clear_health() -> None:
    _HEALTH.clear()


# --- Future scheduler interface (prepared, not implemented) -----------------
@dataclass
class RefreshJob:
    """A planned periodic refresh — consumed by a future scheduler."""

    key: str
    interval_seconds: int
    market_gated: bool
    handler: Optional[Callable[[], None]] = None


class RefreshScheduler:
    """Interface for a future background refresh scheduler.

    Phase 5.1 only *prepares* this: ``planned_jobs`` enumerates what a scheduler
    (cron, Vercel Cron, APScheduler, etc.) would run. Nothing executes yet.
    """

    def __init__(self, settings=None) -> None:
        self.settings = settings

    def planned_jobs(self) -> list[RefreshJob]:
        jobs: list[RefreshJob] = []
        for key in sorted(DEFAULT_REFRESH_POLICIES):
            jobs.append(
                RefreshJob(
                    key=key,
                    interval_seconds=get_refresh_seconds(key, self.settings),
                    market_gated=key in MARKET_GATED_KEYS,
                )
            )
        return jobs

    def start(self) -> None:  # pragma: no cover - intentionally not implemented
        raise NotImplementedError(
            "Background scheduling is not enabled in Phase 5.1; jobs are planned "
            "via planned_jobs() for a future scheduler."
        )
