"""Lightweight smoke tests (no pytest dependency).

Run from the backend directory:

    ./.venv/bin/python -m tests.smoke_test

Covers:
  - /health works
  - /market/usdmxn returns data (mock by default)
  - market_data source tagging: mock | live | fallback
  - /analysis/usdmxn still works end to end
"""

import sys

from fastapi.testclient import TestClient

from app.config import Settings
from app.main import app
from app.services import calendar as calendar_svc
from app.services import market_data
from app.services import news as news_svc
from app.services.secrets import scrub

_passed = 0
_failed = 0


def check(name, fn):
    global _passed, _failed
    try:
        fn()
        _passed += 1
        print(f"  PASS  {name}")
    except Exception as exc:  # noqa: BLE001
        _failed += 1
        print(f"  FAIL  {name}: {exc}")


class _FakeResponse:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self._payload


def test_endpoints():
    # TestClient as a context manager triggers the lifespan (table creation).
    with TestClient(app) as c:
        def health_ok():
            r = c.get("/health")
            assert r.status_code == 200, r.status_code
            assert r.json()["status"] == "ok"

        def market_ok():
            r = c.get("/market/usdmxn")
            assert r.status_code == 200, r.status_code
            body = r.json()
            assert body["pair"] == "USDMXN"
            assert body["usdmxn"] is not None
            assert body["source"] in {"mock", "live", "fallback"}
            for key in (
                "inverse_usdmxn",
                "dxy",
                "us2y",
                "us10y",
                "oil",
                "gold",
                "sp_futures",
                "vix",
                "provider",
            ):
                assert key in body, f"market missing {key}"

        def analysis_ok():
            r = c.get("/analysis/usdmxn")
            assert r.status_code == 200, r.status_code
            body = r.json()
            assert body["direction"] in {"BUY_USD", "SELL_USD", "NO_TRADE"}
            for key in (
                "trade_score",
                "market_bias",
                "confidence",
                "momentum_status",
                "historical_similarity",
                "risk_level",
                "summary",
                "key_drivers",
                "entry",
                "target",
                "stretch_target",
                "stop",
                "expected_move",
                "expected_duration",
                "invalidation_level",
                "risk_notes",
                "timeline",
                "market_drivers",
                "bullish_factors",
                "bearish_factors",
                "upcoming_risks",
            ):
                assert key in body, f"analysis missing {key}"
            assert body["market"]["source"] in {"mock", "live", "fallback"}
            assert isinstance(body["timeline"], list)
            assert isinstance(body["market_drivers"], list)
            assert isinstance(body["bullish_factors"], list)
            assert isinstance(body["bearish_factors"], list)
            assert isinstance(body["upcoming_risks"], list)
            assert "released_last_24h" in body["context"]

        def news_ok():
            r = c.get("/news/recent")
            assert r.status_code == 200, r.status_code
            body = r.json()
            assert body["count"] >= 1
            item = body["news"][0]
            for key in (
                "headline",
                "summary",
                "source",
                "url",
                "published_at",
                "sentiment",
                "affected_currencies",
                "importance",
                "tags",
            ):
                assert key in item, f"news item missing {key}"

        def calendar_ok():
            r = c.get("/calendar/upcoming")
            assert r.status_code == 200, r.status_code
            body = r.json()
            assert body["count"] >= 1
            ev = body["events"][0]
            for key in (
                "event",
                "country",
                "release_time",
                "importance",
                "currency_impact",
                "status",
            ):
                assert key in ev, f"calendar event missing {key}"
            assert ev["status"] == "upcoming"

        def timeline_ok():
            r = c.get("/timeline/usdmxn")
            assert r.status_code == 200, r.status_code
            body = r.json()
            assert body["pair"] == "USDMXN"
            assert isinstance(body["timeline"], list)

        check("/health returns ok", health_ok)
        check("/market/usdmxn returns expanded data", market_ok)
        check("/analysis/usdmxn returns full Phase 3 schema", analysis_ok)
        check("/news/recent returns structured news", news_ok)
        check("/calendar/upcoming returns events", calendar_ok)
        check("/timeline/usdmxn returns timeline", timeline_ok)


def test_source_tagging():
    def source_mock():
        s = Settings(use_mock_data=True)
        data = market_data.get_market_data(s)
        assert data.source == "mock", data.source
        assert data.usdmxn is not None

    def source_fallback_no_key():
        s = Settings(use_mock_data=False, fx_api_key=None)
        data = market_data.get_market_data(s)
        assert data.source == "fallback", data.source
        assert data.usdmxn is not None

    def source_live_ok():
        original = market_data.httpx.get
        market_data.httpx.get = lambda *a, **k: _FakeResponse({"rates": {"MXN": 18.42}})
        try:
            s = Settings(use_mock_data=False, fx_api_key="test-key")
            data = market_data.get_market_data(s)
            assert data.source == "live", data.source
            assert data.usdmxn == 18.42, data.usdmxn
        finally:
            market_data.httpx.get = original

    def source_fallback_on_error():
        original = market_data.httpx.get

        def boom(*a, **k):
            raise RuntimeError("network down")

        market_data.httpx.get = boom
        try:
            s = Settings(use_mock_data=False, fx_api_key="test-key")
            data = market_data.get_market_data(s)
            assert data.source == "fallback", data.source
            assert data.usdmxn is not None
        finally:
            market_data.httpx.get = original

    check("source=mock when USE_MOCK_DATA=true", source_mock)
    check("source=fallback when live wanted but no key", source_fallback_no_key)
    check("source=live on successful fetch", source_live_ok)
    check("source=fallback when fetch raises", source_fallback_on_error)


def test_news_provider():
    SECRET = "news-secret-key-123"

    def news_mock_when_no_key():
        p = news_svc.get_news_provider(Settings(use_mock_data=False, news_api_key=None))
        assert isinstance(p, news_svc.MockNewsProvider), type(p)
        assert p.source == "mock"

    def news_live_ok():
        articles = {
            "status": "ok",
            "articles": [
                {
                    "title": "Fed signals higher-for-longer as CPI runs hot",
                    "description": "Treasury yields climbed after inflation data.",
                    "source": {"name": "Reuters"},
                    "url": "https://example.com/a",
                    "publishedAt": "2026-06-25T12:00:00Z",
                }
            ],
        }
        original = news_svc.httpx.get
        news_svc.httpx.get = lambda *a, **k: _FakeResponse(articles)
        try:
            p = news_svc.get_news_provider(
                Settings(use_mock_data=False, news_api_key=SECRET)
            )
            items = p.get_news()
            assert p.source == "live", p.source
            assert items and items[0]["headline"].startswith("Fed signals")
            assert items[0]["importance"] == "high"  # CPI/Fed -> high
            for key in ("sentiment", "affected_currencies", "importance", "tags"):
                assert key in items[0]
        finally:
            news_svc.httpx.get = original

    def news_fallback_on_error():
        def boom(*a, **k):
            raise RuntimeError(f"network blew up with {SECRET} in the message")

        original = news_svc.httpx.get
        news_svc.httpx.get = boom
        try:
            p = news_svc.get_news_provider(
                Settings(use_mock_data=False, news_api_key=SECRET)
            )
            items = p.get_news()
            assert p.source == "fallback", p.source
            assert items, "fallback should still return mock news"
        finally:
            news_svc.httpx.get = original

    def news_provider_scrubs_secret():
        def boom(*a, **k):
            raise RuntimeError(f"boom url contains app_id={SECRET}")

        original = news_svc.httpx.get
        news_svc.httpx.get = boom
        try:
            prov = news_svc.NewsAPIProvider(
                Settings(use_mock_data=False, news_api_key=SECRET)
            )
            try:
                prov.get_news()
                raise AssertionError("expected RuntimeError")
            except RuntimeError as exc:
                assert SECRET not in str(exc), "secret leaked into error message"
        finally:
            news_svc.httpx.get = original

    check("news mock when live wanted but no key", news_mock_when_no_key)
    check("news source=live on successful fetch", news_live_ok)
    check("news source=fallback when fetch raises", news_fallback_on_error)
    check("news provider scrubs secret from errors", news_provider_scrubs_secret)


def test_calendar_provider():
    SECRET = "cal-secret-key-456"

    def cal_mock_when_no_key():
        p = calendar_svc.get_calendar_provider(
            Settings(use_mock_data=False, calendar_api_key=None)
        )
        assert isinstance(p, calendar_svc.MockCalendarProvider), type(p)
        assert p.source == "mock"

    def cal_live_ok():
        rows = [
            {
                "Country": "United States",
                "Event": "Inflation Rate YoY",
                "Date": "2026-06-25T12:30:00",
                "Actual": "3.4%",
                "Forecast": "3.3%",
                "Previous": "3.5%",
                "Importance": 3,
            },
            {
                "Country": "Mexico",
                "Event": "GDP Growth Rate QoQ",
                "Date": "2026-06-28T12:00:00",
                "Actual": "",
                "Forecast": "0.3%",
                "Previous": "0.2%",
                "Importance": 2,
            },
        ]
        original = calendar_svc.httpx.get
        calendar_svc.httpx.get = lambda *a, **k: _FakeResponse(rows)
        try:
            p = calendar_svc.get_calendar_provider(
                Settings(use_mock_data=False, calendar_api_key=SECRET)
            )
            events = p.get_events()
            assert p.source == "live", p.source
            us = [e for e in events if e["country"] == "US"][0]
            assert us["importance"] == "high"
            assert us["status"] == "released"
            mx = [e for e in events if e["country"] == "MX"][0]
            assert mx["currency_impact"] == "MXN"
            assert mx["status"] == "upcoming"
        finally:
            calendar_svc.httpx.get = original

    def cal_fallback_on_error():
        def boom(*a, **k):
            raise RuntimeError(f"calendar down ({SECRET})")

        original = calendar_svc.httpx.get
        calendar_svc.httpx.get = boom
        try:
            p = calendar_svc.get_calendar_provider(
                Settings(use_mock_data=False, calendar_api_key=SECRET)
            )
            events = p.get_events()
            assert p.source == "fallback", p.source
            assert events, "fallback should return mock events"
        finally:
            calendar_svc.httpx.get = original

    def cal_provider_scrubs_secret():
        def boom(*a, **k):
            raise RuntimeError(f"fail c={SECRET}")

        original = calendar_svc.httpx.get
        calendar_svc.httpx.get = boom
        try:
            prov = calendar_svc.TradingEconomicsCalendarProvider(
                Settings(use_mock_data=False, calendar_api_key=SECRET)
            )
            try:
                prov.get_events()
                raise AssertionError("expected RuntimeError")
            except RuntimeError as exc:
                assert SECRET not in str(exc), "secret leaked into error message"
        finally:
            calendar_svc.httpx.get = original

    check("calendar mock when live wanted but no key", cal_mock_when_no_key)
    check("calendar source=live on successful fetch", cal_live_ok)
    check("calendar source=fallback when fetch raises", cal_fallback_on_error)
    check("calendar provider scrubs secret from errors", cal_provider_scrubs_secret)


def test_scrub():
    def scrubs():
        out = scrub("token=abc123 failed", "abc123")
        assert "abc123" not in out
        assert "***REDACTED***" in out

    def scrubs_noop_without_secret():
        assert scrub("nothing secret here", None) == "nothing secret here"

    check("scrub redacts secret", scrubs)
    check("scrub is a no-op without a secret", scrubs_noop_without_secret)


def main():
    print("Running AI Trading Assistant smoke tests...")
    test_endpoints()
    test_source_tagging()
    test_news_provider()
    test_calendar_provider()
    test_scrub()
    print(f"\n{_passed} passed, {_failed} failed")
    sys.exit(1 if _failed else 0)


if __name__ == "__main__":
    main()
