"""Modular news provider system.

Returns structured news items so the analysis engine and stored snapshots have a
consistent shape regardless of the source. Output schema per item:

    headline, summary, source, url, published_at (ISO),
    sentiment ("usd_bullish" | "mxn_bullish" | "neutral"),  # placeholder
    affected_currencies (list, e.g. ["USD", "MXN"]),
    importance ("high" | "medium" | "low"),
    relevance_score (int 0..100 — USD/MXN relevance; 0 items are discarded),
    tags (list)

Providers
---------
- ``MockNewsProvider``    — realistic offline data; the default and the fallback.
- ``NewsAPIProvider``     — live, backed by NewsAPI.org.
- ``FinnhubNewsProvider`` — live, backed by Finnhub (``NEWS_PROVIDER=finnhub``).
- ``FMPNewsProvider``     — interface stub for future use.

Selection (``get_news_provider``):
- ``USE_MOCK_DATA=true`` or no ``NEWS_API_KEY`` -> ``MockNewsProvider`` (source "mock").
- otherwise a ``ResilientNewsProvider`` that tries the configured live provider
  and falls back to mock data (source "live" on success, "fallback" on error).

Live items are filtered to USD/MXN-relevant topics (Fed/FOMC/Powell, Banxico,
Mexico, peso, USD/MXN, CPI/PPI/NFP, Treasury/inflation, oil, tariffs/US–Mexico
trade); anything scoring 0 relevance is discarded.

Sentiment is a **placeholder**: a light lexical guess that defaults to
"neutral". Real sentiment scoring lands in a later phase.
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from datetime import datetime, timedelta, timezone

import httpx

from app.config import Settings, get_settings
from app.services.secrets import scrub

logger = logging.getLogger(__name__)


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat()


def _epoch_to_iso(epoch) -> str:
    """Convert a Unix timestamp (Finnhub `datetime`) to an ISO-8601 string."""
    try:
        return _iso(datetime.fromtimestamp(float(epoch), tz=timezone.utc))
    except (TypeError, ValueError, OverflowError, OSError):
        return _iso(datetime.now(timezone.utc))


# Topics that move USD/MXN — used to build the live query and to tag items.
NEWS_KEYWORDS = [
    "USD",
    "MXN",
    "Federal Reserve",
    "Banxico",
    "CPI",
    "Inflation",
    "Treasury",
    "Oil",
    "Tariffs",
    "Trade",
    "Mexico",
    "United States",
]

# Phrase -> (tag, affected currency) used for light classification of live items.
_TAG_RULES = [
    ("federal reserve", "fed", "USD"),
    ("fed ", "fed", "USD"),
    ("fomc", "fed", "USD"),
    ("rate cut", "rates", "USD"),
    ("rate hike", "rates", "USD"),
    ("treasury", "rates", "USD"),
    ("yield", "rates", "USD"),
    ("cpi", "inflation", "USD"),
    ("inflation", "inflation", None),
    ("payroll", "jobs", "USD"),
    ("jobs", "jobs", "USD"),
    ("dollar", "usd", "USD"),
    ("banxico", "central-bank", "MXN"),
    ("peso", "mxn", "MXN"),
    ("mexico", "mexico", "MXN"),
    ("tariff", "trade", "MXN"),
    ("trade", "trade", None),
    ("oil", "oil", "MXN"),
    ("crude", "oil", "MXN"),
]

_HIGH_IMPORTANCE = ("cpi", "fomc", "federal reserve", "rate decision", "banxico",
                    "payroll", "nonfarm", "inflation")
_MEDIUM_IMPORTANCE = ("treasury", "yield", "tariff", "gdp", "retail sales",
                      "mexico", "oil")

# Very light, clearly-placeholder sentiment lexicon (defaults to neutral).
_USD_BULL = ("rate hike", "hawkish", "strong jobs", "hot inflation",
             "stronger dollar", "yields rise", "yields climb")
_MXN_BULL = ("rate cut", "dovish", "peso gains", "peso strengthens",
             "oil rises", "risk-on", "tariffs lifted")

# USD/MXN relevance lexicon: (phrase, weight). An article's relevance_score is
# the summed weight of distinct matched phrases, capped at 100. Items scoring 0
# are unrelated financial news and get discarded from live feeds.
_RELEVANCE_TERMS = [
    ("usd/mxn", 50), ("usdmxn", 50), ("dollar-peso", 50), ("peso", 35),
    ("banxico", 40), ("mexico", 30), ("mexican", 25), ("mxn", 30),
    ("u.s.-mexico", 40), ("us-mexico", 40), ("tariff", 30),
    ("federal reserve", 35), ("fomc", 35), ("powell", 30), ("fed ", 25),
    ("interest rate", 20), ("rate cut", 22), ("rate hike", 22),
    ("cpi", 25), ("ppi", 22), ("inflation", 20), ("nonfarm", 25),
    ("payroll", 22), ("nfp", 25), ("jobs report", 22),
    ("treasury", 22), ("yield", 18), ("dxy", 22), ("dollar index", 25),
    ("dollar", 15), ("oil", 18), ("crude", 18), ("wti", 18),
    ("trade deal", 20), ("trade war", 22),
]


def _relevance(text: str) -> int:
    """Score USD/MXN relevance 0..100 from matched topic phrases."""
    score = 0
    for phrase, weight in _RELEVANCE_TERMS:
        if phrase in text:
            score += weight
    return min(100, score)


class NewsProvider(ABC):
    source = "base"

    @abstractmethod
    def get_news(self) -> list[dict]:
        raise NotImplementedError


def _classify(headline: str, summary: str) -> dict:
    text = f"{headline} {summary}".lower()

    tags: list[str] = []
    currencies: set[str] = set()
    for phrase, tag, ccy in _TAG_RULES:
        if phrase in text:
            if tag not in tags:
                tags.append(tag)
            if ccy:
                currencies.add(ccy)
    # Anything in our universe affects USD/MXN broadly.
    if not currencies:
        currencies.update({"USD", "MXN"})

    if any(k in text for k in _HIGH_IMPORTANCE):
        importance = "high"
    elif any(k in text for k in _MEDIUM_IMPORTANCE):
        importance = "medium"
    else:
        importance = "low"

    # Placeholder sentiment: default neutral, nudged only by obvious phrases.
    sentiment = "neutral"
    if any(p in text for p in _USD_BULL):
        sentiment = "usd_bullish"
    elif any(p in text for p in _MXN_BULL):
        sentiment = "mxn_bullish"

    return {
        "sentiment": sentiment,
        "affected_currencies": sorted(currencies),
        "importance": importance,
        "relevance_score": _relevance(text),
        "tags": tags or ["macro"],
    }


class MockNewsProvider(NewsProvider):
    source = "mock"

    def get_news(self) -> list[dict]:
        now = datetime.now(timezone.utc)
        items = [
            {
                "headline": "US yields tick higher after firm jobs data",
                "summary": "Stronger-than-expected payrolls lifted front-end yields, "
                "supporting the dollar broadly.",
                "source": "MockWire",
                "url": "https://example.com/news/us-yields",
                "published_at": _iso(now - timedelta(hours=1)),
                "sentiment": "usd_bullish",
                "affected_currencies": ["USD", "MXN"],
                "importance": "high",
                "tags": ["USD", "rates", "jobs"],
            },
            {
                "headline": "Banxico holds rate, signals data-dependent stance",
                "summary": "Mexico's central bank kept policy steady, citing sticky "
                "core inflation; tone seen broadly neutral for MXN.",
                "source": "MockWire",
                "url": "https://example.com/news/banxico",
                "published_at": _iso(now - timedelta(hours=3)),
                "sentiment": "neutral",
                "affected_currencies": ["MXN"],
                "importance": "medium",
                "tags": ["MXN", "central-bank"],
            },
            {
                "headline": "Oil firms as risk appetite improves",
                "summary": "Crude gained on improving global demand sentiment, a mild "
                "tailwind for the peso.",
                "source": "MockWire",
                "url": "https://example.com/news/oil",
                "published_at": _iso(now - timedelta(hours=5)),
                "sentiment": "mxn_bullish",
                "affected_currencies": ["MXN"],
                "importance": "low",
                "tags": ["oil", "risk"],
            },
        ]
        for it in items:
            it.setdefault(
                "relevance_score",
                _relevance(f"{it['headline']} {it['summary']}".lower()),
            )
        return items


class NewsAPIProvider(NewsProvider):
    """Live news via NewsAPI.org (`/v2/everything`).

    The API key is sent in the ``X-Api-Key`` header so it never appears in a URL
    or any error string. Raises on any failure so the resilient wrapper can fall
    back to mock data.
    """

    source = "newsapi"
    DEFAULT_BASE_URL = "https://newsapi.org/v2/everything"
    PAGE_SIZE = 40

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.base_url = settings.news_base_url or self.DEFAULT_BASE_URL
        self.timeout = settings.http_timeout_seconds

    @staticmethod
    def _query() -> str:
        # Quote multi-word phrases; OR them together.
        parts = [f'"{k}"' if " " in k else k for k in NEWS_KEYWORDS]
        return " OR ".join(parts)

    def get_news(self) -> list[dict]:
        if not self.settings.news_api_key:
            raise RuntimeError("NEWS_API_KEY is not configured.")

        headers = {"X-Api-Key": self.settings.news_api_key}
        params = {
            "q": self._query(),
            "language": "en",
            "sortBy": "publishedAt",
            "pageSize": self.PAGE_SIZE,
        }
        try:
            resp = httpx.get(
                self.base_url, params=params, headers=headers, timeout=self.timeout
            )
            resp.raise_for_status()
            data = resp.json()
        except Exception as exc:  # noqa: BLE001 - re-raise scrubbed
            raise RuntimeError(
                f"News request failed: {scrub(str(exc), self.settings.news_api_key)}"
            ) from None

        if isinstance(data, dict) and data.get("status") == "error":
            # NewsAPI returns the key only if you put it in the URL (we don't),
            # but scrub defensively anyway.
            msg = scrub(str(data.get("message", data)), self.settings.news_api_key)
            raise RuntimeError(f"News provider error: {msg}")

        articles = (data or {}).get("articles") or []
        items: list[dict] = []
        for art in articles:
            headline = (art.get("title") or "").strip()
            if not headline or headline.lower() == "[removed]":
                continue
            summary = (art.get("description") or "").strip()
            meta = _classify(headline, summary)
            if meta["relevance_score"] <= 0:
                continue  # discard unrelated financial news
            items.append(
                {
                    "headline": headline,
                    "summary": summary,
                    "source": ((art.get("source") or {}).get("name") or "NewsAPI"),
                    "url": art.get("url") or "",
                    "published_at": art.get("publishedAt") or _iso(
                        datetime.now(timezone.utc)
                    ),
                    **meta,
                }
            )
        if not items:
            raise RuntimeError("News provider returned no usable articles.")
        items.sort(key=lambda i: i.get("relevance_score", 0), reverse=True)
        return items


class FinnhubNewsProvider(NewsProvider):
    """Live financial news via Finnhub (``/api/v1/news``).

    Pulls the ``general`` + ``forex`` categories, maps them to the shared
    schema, filters to USD/MXN-relevant topics, and ranks by relevance. The API
    key is sent in the ``X-Finnhub-Token`` header so it never appears in a URL
    or error string. Raises on any failure so the resilient wrapper can fall
    back to mock data.
    """

    source = "finnhub"
    DEFAULT_BASE_URL = "https://finnhub.io/api/v1/news"
    CATEGORIES = ("general", "forex")

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.base_url = settings.news_base_url or self.DEFAULT_BASE_URL
        self.timeout = settings.http_timeout_seconds

    def _fetch_category(self, category: str) -> list[dict]:
        headers = {"X-Finnhub-Token": self.settings.news_api_key}
        try:
            resp = httpx.get(
                self.base_url,
                params={"category": category},
                headers=headers,
                timeout=self.timeout,
            )
            resp.raise_for_status()
            data = resp.json()
        except Exception as exc:  # noqa: BLE001 - re-raise scrubbed
            raise RuntimeError(
                f"News request failed: {scrub(str(exc), self.settings.news_api_key)}"
            ) from None
        if isinstance(data, dict) and data.get("error"):
            raise RuntimeError(
                f"News provider error: "
                f"{scrub(str(data.get('error')), self.settings.news_api_key)}"
            )
        return data if isinstance(data, list) else []

    def get_news(self) -> list[dict]:
        if not self.settings.news_api_key:
            raise RuntimeError("NEWS_API_KEY is not configured.")

        raw: list[dict] = []
        for category in self.CATEGORIES:
            raw.extend(self._fetch_category(category))

        items: list[dict] = []
        seen: set[str] = set()
        for art in raw:
            if not isinstance(art, dict):
                continue
            headline = (art.get("headline") or "").strip()
            if not headline:
                continue
            key = headline.lower()
            if key in seen:
                continue
            seen.add(key)
            summary = (art.get("summary") or "").strip()
            meta = _classify(headline, summary)
            if meta["relevance_score"] <= 0:
                continue  # discard unrelated financial news
            items.append(
                {
                    "headline": headline,
                    "summary": summary,
                    "source": (art.get("source") or "Finnhub"),
                    "url": art.get("url") or "",
                    "published_at": _epoch_to_iso(art.get("datetime")),
                    **meta,
                }
            )
        if not items:
            raise RuntimeError("News provider returned no relevant articles.")
        items.sort(key=lambda i: i.get("relevance_score", 0), reverse=True)
        return items


class FMPNewsProvider(NewsProvider):  # pragma: no cover - future stub
    source = "fmp"

    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    def get_news(self) -> list[dict]:
        raise NotImplementedError(
            "Financial Modeling Prep news provider not implemented yet."
        )


# Registry so new providers plug in via NEWS_PROVIDER without touching callers.
_LIVE_NEWS_PROVIDERS = {
    "newsapi": NewsAPIProvider,
    "finnhub": FinnhubNewsProvider,
    "fmp": FMPNewsProvider,
}


class ResilientNewsProvider(NewsProvider):
    """Wraps a live provider with mock fallback and dynamic source tagging.

    ``.source`` is ``"live"`` after a successful live fetch, otherwise
    ``"fallback"``. It is only meaningful after ``get_news()`` has been called.
    """

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.source = "fallback"
        live_cls = _LIVE_NEWS_PROVIDERS.get(
            (settings.news_provider or "newsapi").lower(), NewsAPIProvider
        )
        self._live = live_cls(settings)
        self._mock = MockNewsProvider()

    def get_news(self) -> list[dict]:
        try:
            items = self._live.get_news()
            self.source = "live"
            return items
        except Exception as exc:  # noqa: BLE001 - degrade gracefully
            logger.warning(
                "Live news fetch failed (%s); using mock fallback.",
                scrub(str(exc), self.settings.news_api_key),
            )
            self.source = "fallback"
            return self._mock.get_news()


def get_news_provider(settings: Settings | None = None) -> NewsProvider:
    settings = settings or get_settings()
    if settings.is_mock or not settings.news_api_key:
        return MockNewsProvider()
    return ResilientNewsProvider(settings)
