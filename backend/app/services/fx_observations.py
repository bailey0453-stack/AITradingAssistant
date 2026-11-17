"""Bounded genuine FIX history, including one executable observation per minute."""
from datetime import datetime, timedelta, timezone
from math import isfinite
from threading import Lock
from time import monotonic
from weakref import WeakKeyDictionary

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.models import MarketSnapshot, ObservedFixQuote

_cache = WeakKeyDictionary()
_lock = Lock()
MAX_ROWS = 60_000


def aware(value):
    try:
        at = value if isinstance(value, datetime) else datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return at.replace(tzinfo=timezone.utc) if at.tzinfo is None else at.astimezone(timezone.utc)
    except (ValueError, TypeError):
        return None


def usable_quote(fix, now):
    try:
        bid, ask, at = float(fix["bid"]), float(fix["ask"]), aware(fix["updated_at"])
        if all(isfinite(v) and v > 0 for v in (bid, ask)) and ask >= bid and at and -2 <= (now-at).total_seconds() <= 30:
            return bid, ask, at
    except (TypeError, ValueError, KeyError):
        pass
    return None


def capture(db, fix, now):
    quote = usable_quote(fix, now)
    if not quote:
        return
    bid, ask, at = quote
    minute = at.replace(second=0, microsecond=0)
    if db.execute(select(ObservedFixQuote.id).where(ObservedFixQuote.minute == minute)).first():
        return
    try:
        with db.begin_nested():
            db.add(ObservedFixQuote(minute=minute, quote_at=at, bid=bid, ask=ask, seen_at=now))
            db.flush()
    except IntegrityError:
        pass  # A concurrent request already retained this minute.


def _queries(since):
    return [
        select(MarketSnapshot.created_at, MarketSnapshot.usdmxn).where(
            MarketSnapshot.pair == "USDMXN", MarketSnapshot.provider == "centroid_fix",
            MarketSnapshot.source == "live", MarketSnapshot.usdmxn > 0, MarketSnapshot.created_at >= since
        ).order_by(MarketSnapshot.created_at.desc()).limit(MAX_ROWS),
        select(ObservedFixQuote.quote_at, (ObservedFixQuote.bid+ObservedFixQuote.ask)/2).where(
            ObservedFixQuote.quote_at >= since
        ).order_by(ObservedFixQuote.quote_at.desc()).limit(MAX_ROWS),
    ]


def points(db, now, *, recent_only=False):
    def read(since):
        return [row for query in _queries(since) for row in db.execute(query).all()]
    if recent_only:
        rows = read(now-timedelta(minutes=65))
    else:
        engine = db.get_bind()
        with _lock:
            cached = _cache.get(engine)
            if cached is None or monotonic()-cached[0] > 300:
                cached = (monotonic(), read(now-timedelta(days=90)))
                _cache[engine] = cached
        rows = cached[1] + read(now-timedelta(minutes=65))
    clean = {}
    for at, value in rows:
        at = aware(at)
        if at and at <= now and value is not None and isfinite(value) and value > 0:
            clean[at] = float(value)
    return sorted(clean.items())[-MAX_ROWS:]
