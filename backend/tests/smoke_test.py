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
from app.services import market_data

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
            ):
                assert key in body, f"analysis missing {key}"
            assert body["market"]["source"] in {"mock", "live", "fallback"}
            assert isinstance(body["timeline"], list)

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
        check("/analysis/usdmxn returns full Phase 2 schema", analysis_ok)
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


def main():
    print("Running AI Trading Assistant smoke tests...")
    test_endpoints()
    test_source_tagging()
    print(f"\n{_passed} passed, {_failed} failed")
    sys.exit(1 if _failed else 0)


if __name__ == "__main__":
    main()
