"""Fresh, genuine headlines and cautious USD/MXN catalyst interpretation.

Headlines never supply an invented price target. Explicit directional language
can confirm or contradict a price forecast; ambiguous shocks suspend it.
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from threading import Lock
from time import monotonic
from urllib.parse import urlsplit
from weakref import WeakKeyDictionary

from sqlalchemy import select

from app.config import get_settings
from app.models import NewsItem
from app.services.news import get_news_provider

_lock = Lock()
_cache = None
_persisted = WeakKeyDictionary()
POLL_SECONDS = 60


def _time(value):
    try:
        value = value if isinstance(value, datetime) else datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)
    except (TypeError, ValueError):
        return None


def _interpret(headline):
    text = " ".join(str(headline).lower().split())
    other_currency = re.search(r"\b(canadian|australian|new zealand|singapore|hong kong) dollar\b|\b(argentine|colombian|chilean|philippine) peso\b", text)
    if other_currency and not re.search(r"usd/mxn|usdmxn|mexican peso|u\.s\. dollar|us dollar|dollar index|federal reserve|\bfed\b|\bbanxico\b", text):
        return None, False
    us = bool(re.search(r"\b(fed|federal reserve|fomc|powell)\b", text))
    mx = bool(re.search(r"\b(banxico|bank of mexico|banco de m[eé]xico)\b", text))
    shock = bool(re.search(r"\b(fed|federal reserve|fomc|powell|banxico|inflation|cpi|payrolls?|nonfarm|tariffs?|war|attack|missiles?|sanctions|geopolitical|embargo)\b", text))
    direction = None
    # Conditional/negated headlines are not confirmed policy decisions. The
    # legacy sentiment field is deliberately ignored ("rate cut" is ambiguous).
    uncertain = bool(re.search(r"\b(may|might|could|expected|expects?|forecast|likely|awaits?|ahead|preview|denies?|not|no)\b", text))
    if not uncertain:
        up = r"(?:rises?|rising|gains?|strengthens?|surges?|climbs?|jumps?|advances?)"
        down = r"(?:falls?|falling|drops?|weakens?|slides?|slumps?|tumbles?|declines?)"
        if re.search(r"\b(?:usd/mxn|usdmxn|dollar(?: index)?)\s+" + up, text) or re.search(r"\b(?:mexican )?peso\s+" + down, text):
            direction = "BUY_USD"
        elif re.search(r"\b(?:usd/mxn|usdmxn|dollar(?: index)?)\s+" + down, text) or re.search(r"\b(?:mexican )?peso\s+" + up, text):
            direction = "SELL_USD"
        elif us != mx:
            actor = r"(?:fed|federal reserve|fomc)" if us else r"(?:banxico|bank of mexico|banco de m[eé]xico)"
            cut = bool(re.search(actor + r"\s+(?:unexpectedly\s+|surprisingly\s+)?(?:cuts?|lowers?)\s+(?:interest\s+|policy\s+)?rates?", text))
            hike = bool(re.search(actor + r"\s+(?:unexpectedly\s+|surprisingly\s+)?(?:raises?|hikes?)\s+(?:interest\s+|policy\s+)?rates?", text))
            if cut != hike:
                direction = ("SELL_USD" if cut else "BUY_USD") if us else ("BUY_USD" if cut else "SELL_USD")
    # Mixed explicit price directions are ambiguous rather than first-match wins.
    if not uncertain and re.search(r"\b(?:dollar|usd/mxn|usdmxn)\s+"+up, text) and re.search(r"\b(?:mexican )?peso\s+"+up, text):
        direction = None
    return direction, shock


def snapshot(db=None, *, settings=None):
    """Poll at most once/minute/process, reject mock fallback, retain first-seen."""
    global _cache
    settings = settings or get_settings()
    key = (settings.news_provider, settings.news_base_url, hash(settings.news_api_key), settings.is_mock)
    with _lock:
        if _cache is None or _cache[0] != key or monotonic() - _cache[1] >= POLL_SECONDS:
            now = datetime.now(timezone.utc)
            provider = get_news_provider(settings)
            try:
                items = provider.get_news()
                live = provider.source == "live"
            except Exception:
                items, live = [], False
            data = {
                "status": "LIVE_POLL" if live else "UNAVAILABLE",
                "provider": settings.news_provider,
                "checked_at": now.isoformat(), "poll_seconds": POLL_SECONDS,
                "delivery_latency": "unverified", "items": items if live else [],
            }
            _cache = (key, monotonic(), data)
        data = _cache[2]
        if db is not None and data["status"] == "LIVE_POLL":
            engine = db.get_bind()
            if _persisted.get(engine) != data["checked_at"]:
                from app.routers.market import store_news_items
                try:
                    # One small deduplication read instead of one DB request per
                    # repeated headline on every minute of the day.
                    titles = [item.get("headline") for item in data["items"] if item.get("headline")]
                    existing = set(db.execute(select(NewsItem.headline, NewsItem.published_at)
                        .where(NewsItem.headline.in_(titles)).limit(1000)).all()) if titles else set()
                    fresh = [item for item in data["items"] if (item.get("headline"),item.get("published_at")) not in existing]
                    store_news_items(db, fresh, provider="live")
                    db.commit()
                    _persisted[engine] = data["checked_at"]
                except Exception:
                    db.rollback()
                    # Do not claim we are accumulating news/reaction history.
                    return {**data, "history_saved": False}
    return {**data, "history_saved": db is not None}


def assess(feed, points, fix, *, now=None):
    now = _time(now or datetime.now(timezone.utc))
    out = {"status": "UNAVAILABLE", "provider": (feed or {}).get("provider"),
           "checked_at": (feed or {}).get("checked_at"), "poll_seconds": POLL_SECONDS,
           "headlines": [], "direction": None, "reaction": None, "wait_reasons": [],
           "target_basis": "Numeric target uses measured price analogs; headline impact is not separately calibrated.",
           "delivery_latency": "Feed delivery latency is unverified; poll cadence is not publication speed."}
    checked = _time((feed or {}).get("checked_at"))
    if not feed or feed.get("status") != "LIVE_POLL" or checked is None or not -5 <= (now-checked).total_seconds() <= 90:
        out["wait_reasons"].append("A current genuine news feed check is required; mock or unavailable news cannot support a signal.")
        return out
    out["status"] = "CHECKED_NO_FRESH_CATALYST"
    published_times = [at for item in feed.get("items") or [] if (at := _time(item.get("published_at"))) and at <= now]
    latest = max(published_times, default=None)
    out["latest_published_at"] = latest.isoformat() if latest else None
    if latest and now-latest > timedelta(hours=2):
        out["wait_reasons"].append("The newest relevant article is over two hours old; a delayed or quiet news feed cannot establish current breaking-news coverage.")
    seen = set()
    invalid_dates = 0
    for item in feed.get("items") or []:
        title = str(item.get("headline") or "").strip()
        published = _time(item.get("published_at"))
        if not published or published > now + timedelta(seconds=30):
            invalid_dates += 1
            continue
        if not title or now-published > timedelta(minutes=20):
            continue
        identity = re.sub(r"\W+", " ", title.lower()).strip()
        if identity in seen:
            continue
        seen.add(identity)
        direction, shock = _interpret(title)
        if not direction and not shock:
            continue
        url = str(item.get("url") or "")
        try:
            parsed = urlsplit(url)
            valid_url = parsed.scheme in {"https", "http"} and bool(parsed.hostname)
        except ValueError:
            valid_url = False
        if not valid_url:
            url = ""
        out["headlines"].append({"headline": title, "source": item.get("source"), "url": url,
            "published_at": published.isoformat(), "age_seconds": max(0, round((now-published).total_seconds())),
            "direction": direction, "high_impact": shock})
    out["headlines"].sort(key=lambda item:item["published_at"], reverse=True)
    out["headlines"] = out["headlines"][:8]
    if invalid_dates:
        out["wait_reasons"].append("Some feed headlines lack a trustworthy publication time; news coverage is uncertain.")
    if not out["headlines"]:
        return out
    out["status"] = "FRESH_CATALYST"
    directions = {item["direction"] for item in out["headlines"] if item["direction"]}
    if len(directions) > 1:
        out["wait_reasons"].append("Fresh headlines imply conflicting USD/MXN directions.")
    if any(item["high_impact"] and not item["direction"] for item in out["headlines"]):
        out["wait_reasons"].append("A fresh high-impact headline has an unclear USD/MXN effect; the price-only target is suspended.")
    if len(directions) == 1:
        out["direction"] = next(iter(directions))
        published = _time(out["headlines"][0]["published_at"])
        # Confirmation is observed price change, not proof the article caused it.
        before = [(at, float(value)) for ts, value in points if (at := _time(ts)) and published-timedelta(minutes=8) <= at <= published]
        bid, ask = float(fix["bid"]), float(fix["ask"])
        mid = (bid+ask)/2
        if before and (now-published).total_seconds() >= 60:
            at, value = max(before)
            change = mid-value
            observed = "BUY_USD" if change > 0 else "SELL_USD" if change < 0 else None
            confirmed = observed == out["direction"] and abs(change) >= max(ask-bid, mid*.00005)
            out["reaction"] = {"change_mxn": round(change, 5), "baseline_at": at.isoformat(), "confirmed": confirmed}
        if not (out.get("reaction") or {}).get("confirmed"):
            out["wait_reasons"].append("The live price reaction has not confirmed the headline-implied direction.")
    return out
