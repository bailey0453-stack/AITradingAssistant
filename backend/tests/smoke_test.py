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
from app.services import macro_data as macro_svc
from app.services import market_data
from app.services import news as news_svc
from app.services import market_regime as market_regime_svc
from app.services import signal_weights as sw
from app.services.ai_analysis import RuleBasedAnalyzer
from app.services.market_data import MarketData
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
                "time_horizons",
                "invalidation_level",
                "risk_notes",
                "timeline",
                "market_drivers",
                "bullish_factors",
                "bearish_factors",
                "upcoming_risks",
                "weighted_contributions",
                "conflicting_signals",
                "signal_breakdown",
                "what_would_change_my_mind",
                "market_regime",
                "opportunity_grade",
                "opportunity_grade_detail",
            ):
                assert key in body, f"analysis missing {key}"
            assert body["market"]["source"] in {"mock", "live", "fallback"}
            assert isinstance(body["timeline"], list)
            assert isinstance(body["market_drivers"], list)
            assert isinstance(body["bullish_factors"], list)
            assert isinstance(body["bearish_factors"], list)
            assert isinstance(body["upcoming_risks"], list)
            assert isinstance(body["weighted_contributions"], list)
            assert isinstance(body["conflicting_signals"], list)
            sb = body["signal_breakdown"]
            for k in ("usd_score", "mxn_score", "net_score", "trade_threshold", "weights_version"):
                assert k in sb, f"signal_breakdown missing {k}"
            assert "weights" in sb, "signal_breakdown should persist active weights"
            assert "released_last_24h" in body["context"]
            # Phase 3.5 reasoning layer
            assert isinstance(body["what_would_change_my_mind"], list)
            reg = body["market_regime"]
            assert reg and reg["primary"] in market_regime_svc.REGIMES, reg
            assert 0 <= reg["confidence"] <= 100
            assert body["opportunity_grade"] in {"A+", "A", "B", "C", "D", "PASS"}
            gd = body["opportunity_grade_detail"]
            assert "score" in gd and isinstance(gd["reasons"], list)
            # Phase 4.5 strategist narrative
            for k in ("executive_summary", "why_this_grade", "why_not_higher",
                      "why_not_lower", "current_trade_view", "trader_action"):
                assert isinstance(body.get(k), str) and body[k], f"missing narrative {k}"
            for k in ("quote_guidance", "risk_watchlist", "invalidation_triggers"):
                assert isinstance(body.get(k), list) and body[k], f"missing list {k}"
            # PASS must mean NO_TRADE and never coexist with a direction.
            grade, direction = body["opportunity_grade"], body["direction"]
            assert (grade == "PASS") == (direction == "NO_TRADE"), (grade, direction)
            # Phase 4 historical layer
            for k in ("historical", "probabilities", "confidence_breakdown"):
                assert k in body, f"analysis missing {k}"
            hist = body["historical"] or {}
            assert "statistics" in hist, "historical missing statistics"
            assert (body.get("historical_similarity") or {}).get("status") == "active"
            cb = body["confidence_breakdown"] or {}
            assert "components" in cb and "signal" in cb["components"]
            # Data-source labeling: every source is explicitly tagged.
            ds = body.get("data_sources") or {}
            for k in ("market", "news", "calendar", "historical"):
                assert k in ds, f"data_sources missing {k}"
            assert ds["market"] in {"mock", "live", "fallback"}, ds
            assert ds["news"] in {"mock", "live", "fallback", "cached"}, ds
            assert ds["calendar"] in {"mock", "live", "fallback", "imported"}, ds
            assert ds["historical"] in {"sample", "backfilled", "live"}, ds
            # Phase 5 evidence engine
            for k in ("explanations", "evidence_summary"):
                assert k in body, f"analysis missing {k}"
            ex = body["explanations"] or {}
            for k in ("trade_score", "confidence", "opportunity_grade",
                      "historical_similarity", "probability"):
                assert isinstance(ex.get(k), str) and ex[k], f"explanation missing {k}"
            # Broad evidence base: nearest-neighbor over many events.
            assert (hist.get("considered") or 0) >= 25, hist.get("considered")
            assert isinstance(cb.get("explanation"), list) and cb["explanation"]
            assert isinstance(cb.get("formula"), str) and cb["formula"]
            assert "inputs" in cb, "confidence_breakdown missing six-input map"
            # Expanded outcome statistics.
            hstats = hist.get("statistics") or {}
            for k in ("best_move", "worst_move", "average_MFE", "average_MAE",
                      "max_drawdown", "reversal_probability", "average_holding_hours"):
                assert k in hstats, f"statistics missing {k}"
            # Evidence-based probabilities carry sample size + CI + basis.
            ev = (body["probabilities"] or {}).get("evidence") or {}
            shaped = [e for e in ev.values() if e]
            assert shaped, "probabilities missing evidence detail"
            sample_e = shaped[0]
            for k in ("value", "sample_size", "confidence_interval", "basis"):
                assert k in sample_e, f"probability evidence missing {k}"
            # Multi-horizon outlook: four horizons, each fully shaped.
            th = body["time_horizons"]
            assert isinstance(th, list) and len(th) == 4, th
            assert [h["horizon"] for h in th] == [
                "1-4 hours", "End of day", "1-2 days", "Beyond 2 days"
            ], th
            for h in th:
                for k in ("bias", "confidence", "target", "stretch_target",
                          "stop", "expected_move", "rationale", "risk_level"):
                    assert k in h, f"horizon missing {k}"
                assert h["bias"] in {"BUY_USD", "SELL_USD", "NO_TRADE"}, h["bias"]
                assert 0 <= h["confidence"] <= 100, h["confidence"]

        def news_ok():
            r = c.get("/news/recent")
            assert r.status_code == 200, r.status_code
            body = r.json()
            assert body["count"] >= 1
            assert body.get("provider") in {"mock", "live", "fallback"}, body.get("provider")
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
            assert body.get("provider") in {"mock", "live", "fallback", "imported"}, body.get("provider")
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

        def history_endpoints_ok():
            re = c.get("/history/events")
            assert re.status_code == 200, re.status_code
            assert re.json()["count"] >= 1, "expected seeded sample events"

            rs = c.get("/history/similar")
            assert rs.status_code == 200, rs.status_code
            sb = rs.json()
            assert "top_matches" in sb and isinstance(sb["top_matches"], list)
            assert sb["considered"] >= 25, "expected a broad evidence base"
            if sb["top_matches"]:
                m = sb["top_matches"][0]
                assert "similarity_score" in m and "windows" in m
                # Phase 5: nearest-neighbor distance + rank.
                assert "distance_score" in m and "rank" in m, m
                assert m["rank"] == 1, m["rank"]

            rst = c.get("/history/statistics")
            assert rst.status_code == 200, rst.status_code
            stats = rst.json()["statistics"]
            for k in ("sample_size", "average_move", "win_rate", "typical_MFE",
                      "typical_MAE", "expected_duration", "best_move", "worst_move",
                      "max_drawdown", "reversal_probability", "average_holding_hours"):
                assert k in stats, f"statistics missing {k}"

            rp = c.get("/history/probabilities")
            assert rp.status_code == 200, rp.status_code
            probs = rp.json()["probabilities"]
            lvls = probs.get("levels", {})
            for k in ("probability_reaches_target_1", "probability_reaches_stretch",
                      "probability_hits_stop", "probability_finishes_positive_today",
                      "probability_finishes_positive_within_5d"):
                assert k in lvls, f"probabilities missing {k}"
            assert "evidence" in probs, "probabilities missing evidence detail"

        check("/health returns ok", health_ok)
        check("/market/usdmxn returns expanded data", market_ok)
        check("/analysis/usdmxn returns full Phase 4 schema", analysis_ok)
        check("/news/recent returns structured news", news_ok)
        check("/calendar/upcoming returns events", calendar_ok)
        check("/timeline/usdmxn returns timeline", timeline_ok)
        check("/history/* endpoints return valid data", history_endpoints_ok)


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


def _market(**drivers) -> MarketData:
    return MarketData(
        pair="USDMXN", usdmxn=17.9, inverse_usdmxn=0.0559, dxy=104.5, us2y=4.7,
        us10y=4.35, treasury_yield=4.35, oil=76.0, gold=2380.0, sp_futures=5450.0,
        vix=15.0, provider="mock", source="mock", drivers=drivers,
    )


def test_signal_weighting():
    def weights_load_defaults():
        w = sw.get_signal_weights(Settings())
        for key in sw.DEFAULT_WEIGHTS:
            assert key in w, f"missing weight {key}"
        assert w["fed_rate_decision"] == 10
        assert w["general_financial_news"] == 4

    def weights_override_and_ignore_unknown():
        s = Settings(signal_weights={"dxy": 99, "not_a_real_key": 1})
        w = sw.get_signal_weights(s)
        assert w["dxy"] == 99, w["dxy"]
        assert "not_a_real_key" not in w

    def scores_usd_bias():
        # Every macro driver pushes USD: DXY up, yields up, oil down, gold down,
        # equities down (risk-off), VIX up.
        m = _market(
            dxy_delta=0.6, yield_delta=0.08, us2y_delta=0.08, oil_delta=-2.5,
            gold_delta=-25.0, sp_delta=-40.0, vix_delta=3.0,
        )
        r = sw.score_signals(m)
        assert r["direction"] == "BUY_USD", r["direction"]
        assert r["usd_score"] > r["mxn_score"]
        assert r["net_score"] > 0
        assert r["weighted_contributions"], "expected contributions"
        assert r["key_drivers"] and "No dominant" not in r["key_drivers"][0]
        assert r["weights_version"] == sw.WEIGHTS_VERSION

    def identifies_conflicts():
        # USD-leaning DXY/yields, but oil + equities lean MXN -> conflicts listed.
        m = _market(
            dxy_delta=0.6, yield_delta=0.08, us2y_delta=0.08, oil_delta=2.5,
            sp_delta=40.0,
        )
        r = sw.score_signals(m)
        if r["direction"] == "BUY_USD":
            opp = {c["direction"] for c in r["conflicting_signals"]}
            assert opp == {"MXN"}, opp
            keys = {c["key"] for c in r["conflicting_signals"]}
            assert "oil" in keys and "sp_futures" in keys, keys

    def weights_actually_change_score():
        m = _market(dxy_delta=0.6)  # single USD driver
        base = sw.score_signals(m, weights=sw.get_signal_weights())
        zeroed = dict(sw.DEFAULT_WEIGHTS)
        zeroed["dxy"] = 0
        muted = sw.score_signals(m, weights=zeroed)
        assert base["usd_score"] > muted["usd_score"], "weight change had no effect"
        assert muted["usd_score"] == 0

    check("weights load defaults", weights_load_defaults)
    check("weights override + ignore unknown keys", weights_override_and_ignore_unknown)
    check("score_signals yields USD bias on USD evidence", scores_usd_bias)
    check("score_signals identifies conflicting signals", identifies_conflicts)
    check("weights are configurable (change the score)", weights_actually_change_score)


def test_reasoning_engine():
    def regime_high_vol_risk_off():
        # Spiking VIX + falling equities -> High/Low Volatility + Risk Off in play.
        m = _market(vix_delta=4.0, sp_delta=-50.0)
        m.vix = 26.0
        reg = market_regime_svc.detect_regime(m)
        assert reg["primary"] in market_regime_svc.REGIMES
        assert reg["primary"] in {"High Volatility", "Risk Off"}, reg["primary"]
        assert 0 <= reg["confidence"] <= 100
        assert reg["scores"], "expected non-empty regime scores"

    def regime_fed_driven_from_calendar():
        m = _market()
        cal = [{"event": "FOMC Rate Decision", "importance": "high", "status": "upcoming"}]
        reg = market_regime_svc.detect_regime(m, calendar=cal)
        assert "Fed Driven" in reg["scores"], reg["scores"]

    def regime_defaults_range_bound():
        # No deltas, calm VIX -> nothing dominant -> range bound.
        m = _market()
        m.vix = 15.0
        reg = market_regime_svc.detect_regime(m)
        assert reg["primary"] in {"Range Bound", "Low Volatility"}, reg["primary"]

    def grade_pass_on_no_trade():
        analyzer = RuleBasedAnalyzer()
        m = _market()  # flat -> NO_TRADE
        out = analyzer.analyze(m)
        assert out["direction"] == "NO_TRADE"
        assert out["opportunity_grade"] == "PASS", out["opportunity_grade"]
        assert out["what_would_change_my_mind"], "should suggest what creates an edge"

    def grade_letters_on_strong_signal():
        analyzer = RuleBasedAnalyzer()
        m = _market(
            dxy_delta=0.6, yield_delta=0.08, us2y_delta=0.08, oil_delta=-2.5,
            gold_delta=-25.0, sp_delta=-40.0, vix_delta=2.0,
        )
        out = analyzer.analyze(m)
        assert out["direction"] == "BUY_USD", out["direction"]
        assert out["opportunity_grade"] in {"A+", "A", "B", "C", "D"}
        assert out["opportunity_grade_detail"]["score"] is not None
        assert out["market_regime"]["primary"] in market_regime_svc.REGIMES

    check("regime: high VIX + risk-off classified", regime_high_vol_risk_off)
    check("regime: Fed event drives Fed Driven", regime_fed_driven_from_calendar)
    check("regime: calm tape -> range bound", regime_defaults_range_bound)
    check("grade: NO_TRADE -> PASS + WWCM populated", grade_pass_on_no_trade)
    check("grade: strong signal earns a letter grade", grade_letters_on_strong_signal)


def test_strategist_narrative():
    from app.services.ai_analysis import RuleBasedAnalyzer
    from app.services.market_data import MockMarketDataProvider

    analyzer = RuleBasedAnalyzer()

    def narrative_fields_and_consistency():
        # Run several mock snapshots so we hit both NO_TRADE and directional cases.
        seen_grades = set()
        for _ in range(12):
            market = MockMarketDataProvider().get_usdmxn()
            r = analyzer.analyze(market)
            for k in ("executive_summary", "why_this_grade", "why_not_higher",
                      "why_not_lower", "current_trade_view", "trader_action"):
                assert isinstance(r[k], str) and r[k], k
            for k in ("quote_guidance", "risk_watchlist", "invalidation_triggers"):
                assert isinstance(r[k], list) and r[k], k
            grade, direction = r["opportunity_grade"], r["direction"]
            # Core consistency rule: PASS iff NO_TRADE.
            assert (grade == "PASS") == (direction == "NO_TRADE"), (grade, direction)
            # Directional reads never grade PASS; they floor at D.
            if direction != "NO_TRADE":
                assert grade in {"A+", "A", "B", "C", "D"}, grade
            seen_grades.add(grade)
        assert seen_grades, "no analysis produced"

    def quote_guidance_event_aware():
        # A high-impact event within 24h should trigger cautious pricing guidance.
        from app.services.market_regime import detect_regime
        from app.services.signals import compute_signal

        market = MockMarketDataProvider().get_usdmxn()
        soon = [{
            "event": "US CPI", "country": "US", "importance": "high",
            "release_time": None, "hours_away": 6.0, "note": "",
        }]
        signal = compute_signal(market)
        regime = detect_regime(market)
        gd = analyzer._opportunity_grade(signal, regime, market)
        brief = analyzer._strategist_narrative(
            market, signal, {"primary": "Fed Driven", "confidence": 55},
            gd, soon, [], [], [], [],
        )
        joined = " ".join(brief["quote_guidance"]).lower()
        assert "high-impact" in joined or "validity short" in joined, brief["quote_guidance"]
        assert any("cpi" in r.lower() for r in brief["risk_watchlist"]), brief["risk_watchlist"]

    def grade_direction_action_rule():
        # Encode the full PASS/C/B/A consistency rule across many mock snapshots.
        for _ in range(40):
            market = MockMarketDataProvider().get_usdmxn()
            r = analyzer.analyze(market)
            grade, direction = r["opportunity_grade"], r["direction"]
            action = (r["trader_action"] or "").lower()

            if grade == "PASS":
                assert direction == "NO_TRADE", (grade, direction)
                assert "do not initiate" in action, action
                # Trade plan must be blank / not applicable.
                assert r["target"] is None and r["stretch_target"] is None and r["stop"] is None, r
            elif grade == "C":
                assert direction in {"BUY_USD", "SELL_USD"}, (grade, direction)
                assert "low-quality setup" in action and "operational need" in action, action
            elif grade in {"B", "A", "A+"}:
                assert direction in {"BUY_USD", "SELL_USD"}, (grade, direction)
            elif grade == "D":
                assert direction in {"BUY_USD", "SELL_USD"}, (grade, direction)

    def action_wording_is_correct():
        # Deterministic regardless of mock randomness.
        amap = analyzer._ACTION_BY_GRADE
        assert "do not initiate" in amap["PASS"].lower()
        c = amap["C"].lower()
        assert "low-quality setup" in c and "operational need" in c, c
        for g in ("B", "A", "A+"):
            assert amap[g], g

    check("narrative fields present + PASS==NO_TRADE consistency", narrative_fields_and_consistency)
    check("trader_action wording satisfies PASS/C rule", action_wording_is_correct)
    check("quote guidance reacts to imminent high-impact event", quote_guidance_event_aware)
    check("PASS/C/B/A grade-direction-action rule holds", grade_direction_action_rule)


def test_multi_horizon():
    from app.services.ai_analysis import RuleBasedAnalyzer
    from app.services.market_data import MockMarketDataProvider

    analyzer = RuleBasedAnalyzer()
    ORDER = ["1-4 hours", "End of day", "1-2 days", "Beyond 2 days"]

    def four_horizons_shaped():
        for _ in range(12):
            market = MockMarketDataProvider().get_usdmxn()
            r = analyzer.analyze(market)
            th = r["time_horizons"]
            assert [h["horizon"] for h in th] == ORDER, th
            for h in th:
                assert h["bias"] in {"BUY_USD", "SELL_USD", "NO_TRADE"}
                assert 0 <= h["confidence"] <= 100
                # Directional horizons carry levels; flat horizons do not.
                if h["bias"] == "NO_TRADE":
                    assert h["target"] is None and h["stop"] is None, h
                else:
                    assert h["target"] is not None and h["stop"] is not None, h

    def swing_not_more_confident_than_intraday_baseline():
        # Beyond-2-days is capped well below the intraday ceiling.
        for _ in range(8):
            market = MockMarketDataProvider().get_usdmxn()
            r = analyzer.analyze(market)
            swing = r["time_horizons"][3]
            assert swing["confidence"] <= 55.0, swing

    def pass_primary_allows_horizon_lean():
        # PASS keeps the *primary* recommendation NO_TRADE, but horizons MAY lean.
        # Construct contributions that net flat overall yet lean short-term USD:
        # strong short-term USD drivers offset by an opposing event signal.
        market = _market(dxy_delta=0.6, yield_delta=0.08, us2y_delta=0.08)
        signal = {
            "direction": "NO_TRADE",
            "confidence": 20.0,
            "trade_score": 5.0,
            "weighted_contributions": [
                {"key": "dxy", "label": "DXY", "direction": "USD",
                 "weight": 8, "strength": 0.9, "contribution": 7.2, "detail": "DXY"},
                {"key": "treasury_yield", "label": "Treasury", "direction": "USD",
                 "weight": 8, "strength": 0.5, "contribution": 4.0, "detail": "yields"},
                {"key": "us_cpi", "label": "US CPI", "direction": "MXN",
                 "weight": 9, "strength": 0.9, "contribution": -8.1, "detail": "CPI"},
            ],
        }
        horizons = analyzer._time_horizons(market, signal, {"primary": "Fed Driven", "confidence": 50}, [])
        intraday = horizons[0]
        # Short horizon de-emphasizes the event -> USD lean emerges.
        assert intraday["bias"] == "BUY_USD", intraday
        # Primary recommendation (overall direction) is unchanged / NO_TRADE.
        assert signal["direction"] == "NO_TRADE"

    check("multi-horizon: four horizons fully shaped", four_horizons_shaped)
    check("multi-horizon: swing confidence capped low", swing_not_more_confident_than_intraday_baseline)
    check("multi-horizon: PASS primary still allows horizon lean", pass_primary_allows_horizon_lean)


def test_history_engine():
    from app.services.history import historical_statistics as hs
    from app.services.history import similarity_engine as sim
    from app.services.history.historical_prices import compute_reaction_windows

    def reaction_windows_math():
        # USD/MXN rises ~1% then settles; baseline 18.0.
        baseline = 18.0
        path = [(0.0, 18.0), (1.0, 18.09), (4.0, 18.18), (24.0, 18.22),
                (72.0, 18.16), (120.0, 18.12)]
        out = compute_reaction_windows(path, baseline)
        assert out["ret_1h"] is not None and out["ret_1h"] > 0
        assert out["ret_5d"] is not None
        # Peak (18.22) is above the 5d close -> MFE should exceed final return.
        assert out["max_favorable_excursion"] >= out["ret_5d"]
        assert out["data_completeness"] == 1.0
        assert out["reversal_behavior"] in {"continuation", "fade", "reversal"}

    def similarity_prefers_closer_context():
        query = {"regime": "Fed Driven", "event_type": "fed_rate_decision",
                 "dxy": 104.0, "vix": 14.0, "us2y": 4.6, "us10y": 4.2,
                 "oil": 80.0, "gold": 2000.0, "sp_futures": 5200.0,
                 "momentum": 0.0, "news_tags": ["fed", "rates"]}
        near = {"event_type": "fed_rate_decision", "context": {
            "regime": "Fed Driven", "dxy": 104.2, "vix": 14.5, "us2y": 4.6,
            "us10y": 4.2, "oil": 81.0, "gold": 2010.0, "sp_futures": 5210.0,
            "momentum": 0.0, "news_tags": ["fed", "rates"]}}
        far = {"event_type": "us_nfp", "context": {
            "regime": "High Volatility", "dxy": 99.0, "vix": 30.0, "us2y": 3.2,
            "us10y": 3.0, "oil": 60.0, "gold": 2500.0, "sp_futures": 4500.0,
            "momentum": 0.2, "news_tags": ["jobs"]}}
        w = sim.get_similarity_weights()
        s_near = sim.score_reaction(query, near, w)
        s_far = sim.score_reaction(query, far, w)
        assert s_near > s_far, (s_near, s_far)
        assert 0.0 <= s_far <= s_near <= 1.0

    def probability_forecast_directional():
        # Two matches that moved +0.5% and +0.9% favorably for a BUY_USD trade.
        matches = [
            {"windows": {"1h": 0.2, "1d": 0.5, "5d": 0.45}},
            {"windows": {"1h": 0.3, "1d": 0.9, "5d": 0.8}},
        ]
        targets = {"target_1": 100.5, "target_2": 100.7, "stretch": 101.0, "stop": 99.6}
        out = hs.probability_forecast(matches, current_price=100.0,
                                      direction="BUY_USD", targets=targets)
        lv = out["levels"]
        # Both reach +0.5%; only one reaches +0.9% (stretch +1.0% none).
        assert lv["probability_reaches_target_1"] == 100.0, lv
        assert lv["probability_reaches_stretch"] == 0.0, lv

    def confidence_blend_renormalizes():
        # Missing historical component should not drag confidence down.
        full = hs.blend_confidence({"signal": 80, "historical": 80, "regime": 80,
                                    "volatility": 80, "data_quality": 80})
        partial = hs.blend_confidence({"signal": 80, "historical": None,
                                       "regime": 80, "volatility": 80,
                                       "data_quality": 80})
        assert full["value"] == 80.0
        assert partial["value"] == 80.0  # renormalized, not penalized
        assert "historical" not in partial["weights_used"]

    def confidence_weights_configurable():
        from app.config import Settings
        s = Settings(confidence_weights={"signal": 1.0, "historical": 0.0,
                                         "regime": 0.0, "volatility": 0.0,
                                         "data_quality": 0.0})
        out = hs.blend_confidence({"signal": 70, "historical": 10, "regime": 10,
                                   "volatility": 10, "data_quality": 10}, settings=s)
        assert out["value"] == 70.0, out  # only signal counts

    check("reaction window math (MFE/returns/completeness)", reaction_windows_math)
    check("similarity ranks closer context higher", similarity_prefers_closer_context)
    check("probability forecast is directional", probability_forecast_directional)
    check("confidence blend renormalizes missing parts", confidence_blend_renormalizes)
    check("confidence weights are configurable", confidence_weights_configurable)


def test_source_labeling():
    import os
    import tempfile

    def news_provider_source_live_vs_mock():
        # No key -> mock provider; with key + live success -> ResilientNews "live".
        p = news_svc.get_news_provider(Settings(use_mock_data=False, news_api_key=None))
        assert p.source == "mock", p.source

        articles = {"status": "ok", "articles": [{
            "title": "Fed holds rates as inflation cools",
            "description": "Dollar steadies after the decision.",
            "source": {"name": "Reuters"}, "url": "https://x/y",
            "publishedAt": "2026-06-27T10:00:00Z",
        }]}
        original = news_svc.httpx.get
        news_svc.httpx.get = lambda *a, **k: _FakeResponse(articles)
        try:
            rp = news_svc.get_news_provider(
                Settings(use_mock_data=False, news_api_key="k")
            )
            rp.get_news()
            assert rp.source == "live", rp.source
        finally:
            news_svc.httpx.get = original

    def csv_calendar_imports_without_key():
        rows = (
            "event,country,release_time,forecast,previous,actual,importance,currency_impact\n"
            "US CPI (MoM),US,2099-01-15T13:30:00,0.3,0.2,,high,USD\n"
            "Banxico Rate Decision,MX,2099-01-20T19:00:00,hold,hold,,high,MXN\n"
        )
        fd, path = tempfile.mkstemp(suffix=".csv")
        os.write(fd, rows.encode()); os.close(fd)
        try:
            # CSV works even in mock mode and with no API key.
            s = Settings(use_mock_data=True, calendar_provider="csv",
                         calendar_csv_path=path, calendar_api_key=None)
            prov = calendar_svc.get_calendar_provider(s)
            events = prov.get_upcoming()
            assert prov.source == "imported", prov.source
            assert any(e["event"].startswith("US CPI") for e in events), events
            assert {e["country"] for e in events} <= {"US", "MX"}
        finally:
            os.remove(path)

    def csv_calendar_falls_back_when_missing():
        s = Settings(use_mock_data=True, calendar_provider="csv",
                     calendar_csv_path="/nonexistent/calendar.csv")
        prov = calendar_svc.get_calendar_provider(s)
        events = prov.get_upcoming()
        assert prov.source == "fallback", prov.source
        assert events, "fallback should still return mock events"

    check("news provider tags mock vs live source", news_provider_source_live_vs_mock)
    check("CSV calendar imports without an API key", csv_calendar_imports_without_key)
    check("CSV calendar falls back to mock when file missing", csv_calendar_falls_back_when_missing)


def test_market_infrastructure():
    from datetime import datetime, timezone

    from app.database import SessionLocal, init_db
    from app.models import HistoricalMarketSnapshot, MarketSnapshot
    from app.routers import market as market_mod
    from app.services import cache_manager
    from app.services import market_hours as mh
    from app.services.market_data import MarketData

    init_db()

    def _state(is_open, status):
        return mh.MarketState(
            market_status=status, market_reason="test", is_open=is_open,
            last_market_close="2026-06-26T21:00:00+00:00",
            next_market_open="2026-06-28T21:00:00+00:00",
            next_expected_refresh="2026-06-28T21:00:00+00:00",
        )

    def market_hours_schedule():
        cal = mh.MarketCalendar()
        sat = datetime(2026, 6, 27, 12, 0, tzinfo=timezone.utc)      # Saturday
        wed = datetime(2026, 6, 24, 12, 0, tzinfo=timezone.utc)      # Wednesday
        fri_late = datetime(2026, 6, 26, 22, 0, tzinfo=timezone.utc)  # Fri after close
        sun_late = datetime(2026, 6, 28, 22, 0, tzinfo=timezone.utc)  # Sun after open
        assert mh.get_market_state(sat, cal).market_status == "WEEKEND"
        assert mh.get_market_state(wed, cal).is_open is True
        assert mh.get_market_state(fri_late, cal).is_open is False
        assert mh.get_market_state(sun_late, cal).is_open is True
        st = mh.get_market_state(sat, cal)
        assert st.next_market_open and st.last_market_close

    def holiday_framework_closes_open_day():
        wed = datetime(2026, 6, 24, 12, 0, tzinfo=timezone.utc)
        cal = mh.MarketCalendar(holidays={wed.date(): "Test Holiday"})
        st = mh.get_market_state(wed, cal)
        assert st.market_status == "HOLIDAY" and st.is_open is False, st

    def gating_rules():
        # USD/MXN is market-gated: closed -> never refresh.
        assert cache_manager.should_refresh("usdmxn", market_open=False, age_seconds=None) is False
        # Open + nothing cached -> refresh.
        assert cache_manager.should_refresh("usdmxn", market_open=True, age_seconds=None) is True
        # Open + within interval -> cache before live.
        assert cache_manager.should_refresh("usdmxn", market_open=True, age_seconds=5) is False
        # News is not market-gated -> flows on the weekend.
        assert cache_manager.should_refresh("news", market_open=False, age_seconds=None) is True

    def weekend_never_requests_usdmxn():
        db = SessionLocal()
        try:
            db.query(MarketSnapshot).delete(); db.commit()
            db.add(MarketSnapshot(pair="USDMXN", usdmxn=17.9, source="live",
                                  provider="seed", sources={"usdmxn": "live"}))
            db.commit()
            called = {"n": 0}
            def boom(*a, **k):
                called["n"] += 1
                raise AssertionError("USD/MXN must not be requested while closed")
            orig_state, orig_md = market_mod.get_market_state, market_mod.get_market_data
            market_mod.get_market_state = lambda **k: _state(False, "WEEKEND")
            market_mod.get_market_data = boom
            try:
                intel = market_mod.get_market_intelligence(db, Settings(use_mock_data=True))
            finally:
                market_mod.get_market_state, market_mod.get_market_data = orig_state, orig_md
            assert called["n"] == 0, "provider was called while market closed"
            assert intel["meta"]["cached"] is True, intel["meta"]
            assert intel["market"].usdmxn == 17.9
        finally:
            db.close()

    def latest_cache_before_mock_on_failure():
        db = SessionLocal()
        try:
            db.query(MarketSnapshot).delete(); db.commit()
            db.add(MarketSnapshot(pair="USDMXN", usdmxn=18.12, source="live",
                                  provider="seed", sources={"usdmxn": "live"}))
            db.commit()
            def boom(*a, **k):
                raise RuntimeError("provider down")
            orig_state, orig_md = market_mod.get_market_state, market_mod.get_market_data
            market_mod.get_market_state = lambda **k: _state(True, "OPEN")
            market_mod.get_market_data = boom
            try:
                # interval 0 forces a refresh attempt that then fails -> serve cache.
                intel = market_mod.get_market_intelligence(
                    db, Settings(use_mock_data=False, fx_api_key="k",
                                 refresh_policies={"usdmxn": 0})
                )
            finally:
                market_mod.get_market_state, market_mod.get_market_data = orig_state, orig_md
            assert intel["meta"]["cached"] is True, intel["meta"]
            assert intel["market"].usdmxn == 18.12, "served latest cache, not mock"
        finally:
            db.close()

    def live_refresh_auto_captures_history():
        db = SessionLocal()
        try:
            db.query(MarketSnapshot).delete()
            db.query(HistoricalMarketSnapshot).delete(); db.commit()
            before = db.query(HistoricalMarketSnapshot).count()
            live = MarketData(pair="USDMXN", usdmxn=18.4, inverse_usdmxn=0.0543,
                              dxy=104.0, us2y=4.7, us10y=4.3, oil=77.0, gold=2380.0,
                              vix=14.0, sp_futures=5450.0, provider="oxr", source="live",
                              field_sources={"usdmxn": "live", "us2y": "live"})
            orig_state, orig_md = market_mod.get_market_state, market_mod.get_market_data
            market_mod.get_market_state = lambda **k: _state(True, "OPEN")
            market_mod.get_market_data = lambda *a, **k: live
            try:
                intel = market_mod.get_market_intelligence(
                    db, Settings(use_mock_data=False, fx_api_key="k")
                )
            finally:
                market_mod.get_market_state, market_mod.get_market_data = orig_state, orig_md
            after = db.query(HistoricalMarketSnapshot).count()
            assert after == before + 1, (before, after)
            row = db.query(HistoricalMarketSnapshot).order_by(
                HistoricalMarketSnapshot.id.desc()).first()
            assert row.source_quality == "live" and row.usdmxn == 18.4, row
            assert intel["meta"]["cached"] is False
            ph = cache_manager.health_snapshot()
            assert ph.get("fx", {}).get("status") == "healthy", ph
        finally:
            db.close()

    check("FX market-hours schedule (weekend/weekday/boundaries)", market_hours_schedule)
    check("holiday framework closes an otherwise-open day", holiday_framework_closes_open_day)
    check("refresh gating (closed=no fetch, within-interval=cache, news ungated)", gating_rules)
    check("weekend never requests USD/MXN (serves cache)", weekend_never_requests_usdmxn)
    check("latest cache served before mock on provider failure", latest_cache_before_mock_on_failure)
    check("live refresh auto-captures a historical snapshot + health", live_refresh_auto_captures_history)


def test_recommendation_tracking():
    from datetime import datetime, timedelta, timezone

    from app.database import SessionLocal, init_db
    from app.models import MarketSnapshot, Recommendation, RecommendationOutcome
    from app.services import recommendation_evaluator as rev

    init_db()

    def analysis_stores_recommendation():
        with TestClient(app) as c:
            before = c.get("/recommendations/recent?limit=1").json()["count"]
            c.get("/analysis/usdmxn")
            body = c.get("/recommendations/recent?limit=5").json()
            assert body["count"] >= 1, body
            r = body["recommendations"][0]
            for k in ("direction", "confidence", "opportunity_grade", "spot_price",
                      "time_horizons", "strategist", "evaluation_status"):
                assert k in r, (k, r)

    def evaluator_scores_old_recommendations():
        db = SessionLocal()
        try:
            db.query(RecommendationOutcome).delete()
            db.query(Recommendation).delete()
            db.query(MarketSnapshot).delete()
            db.commit()

            now = datetime.now(timezone.utc)
            t0 = now - timedelta(days=6)
            t0 = t0.replace(hour=10, minute=0, second=0, microsecond=0)

            reco = Recommendation(
                pair="USDMXN", created_at=t0, spot_price=18.00,
                direction="BUY_USD", confidence=80.0, opportunity_grade="B",
                trade_score=70.0, target=18.15, stretch_target=18.30, stop=17.90,
            )
            db.add(reco)
            # Entry price + a price at each horizon's due time.
            db.add(MarketSnapshot(pair="USDMXN", usdmxn=18.00, created_at=t0))
            prices = {"1h": 18.05, "4h": 18.12, "end_of_day": 18.14,
                      "1d": 18.20, "2d": 18.16, "5d": 18.40}
            for h, px in prices.items():
                due = rev.horizon_due_time(t0, h)
                db.add(MarketSnapshot(pair="USDMXN", usdmxn=px, created_at=due))
            db.commit()

            result = rev.evaluate_due(db, now=now)
            assert result["evaluated"] == 6, result

            outs = {o.horizon: o for o in db.query(RecommendationOutcome)
                    .filter(RecommendationOutcome.recommendation_id == reco.id).all()}
            assert set(outs) == set(prices), list(outs)
            assert outs["1h"].direction_correct is True
            assert outs["1h"].target_hit is False, "target not hit within 1h"
            assert outs["1d"].target_hit is True, "target hit by 1d"
            assert outs["5d"].stretch_hit is True, "stretch hit by 5d"
            assert all(o.stop_hit is False for o in outs.values())
            assert abs(outs["1d"].return_pct - round((18.20-18.00)/18.00*100, 4)) < 1e-6
            assert (outs["1d"].max_favorable_excursion or 0) > 0

            db.refresh(reco)
            assert reco.evaluation_status == "complete", reco.evaluation_status
        finally:
            db.close()

    def performance_summary_is_fast_read():
        db = SessionLocal()
        try:
            perf = rev.performance_summary(db)
            for k in ("total_recommendations", "evaluated_outcomes", "win_rate",
                      "target_hit_rate", "stop_hit_rate", "by_confidence",
                      "by_grade", "by_horizon"):
                assert k in perf, (k, perf)
            assert perf["evaluated_outcomes"] >= 6, perf
            assert set(perf["by_horizon"]) == set(rev.HORIZONS), perf["by_horizon"]
        finally:
            db.close()

    def performance_endpoint_ok():
        with TestClient(app) as c:
            r = c.get("/recommendations/performance")
            assert r.status_code == 200, r.status_code
            assert "by_horizon" in r.json()
            ev = c.post("/recommendations/evaluate")
            assert ev.status_code == 200 and "evaluated" in ev.json(), ev.text

    check("every analysis stores a paper recommendation", analysis_stores_recommendation)
    check("evaluator scores old recommendations across horizons", evaluator_scores_old_recommendations)
    check("performance summary is a fast aggregate read", performance_summary_is_fast_read)
    check("recommendation endpoints respond", performance_endpoint_ok)


def test_research_lab():
    from datetime import datetime, timedelta, timezone

    from app.database import SessionLocal, init_db
    from app.models import MarketSnapshot, Recommendation, RecommendationOutcome
    from app.services import recommendation_evaluator as rev
    from app.services import research_lab as rl

    init_db()

    def seed_and_evaluate():
        db = SessionLocal()
        try:
            db.query(RecommendationOutcome).delete()
            db.query(Recommendation).delete()
            db.query(MarketSnapshot).delete()
            db.commit()

            now = datetime.now(timezone.utc)
            t0 = (now - timedelta(days=6)).replace(hour=10, minute=0, second=0, microsecond=0)

            buy = Recommendation(
                pair="USDMXN", created_at=t0, spot_price=18.00, direction="BUY_USD",
                confidence=88.0, opportunity_grade="A", regime="Trending",
                model_version="9.9-test", target=18.15, stretch_target=18.30, stop=17.90,
                key_drivers=["DXY momentum", "US yields"],
            )
            no_trade = Recommendation(
                pair="USDMXN", created_at=t0, spot_price=18.00, direction="NO_TRADE",
                confidence=40.0, opportunity_grade="PASS", regime="Range Bound",
                model_version="9.9-test",
            )
            db.add_all([buy, no_trade])
            db.add(MarketSnapshot(pair="USDMXN", usdmxn=18.00, created_at=t0))
            for h, px in {"1h": 18.05, "4h": 18.12, "end_of_day": 18.14, "1d": 18.20}.items():
                db.add(MarketSnapshot(pair="USDMXN", usdmxn=px, created_at=rev.horizon_due_time(t0, h)))
            db.commit()
            rev.evaluate_due(db, now=now)
            return buy.id, no_trade.id
        finally:
            db.close()

    buy_id, no_trade_id = seed_and_evaluate()

    def paper_hedge_math_is_correct():
        db = SessionLocal()
        try:
            buy_1d = db.query(RecommendationOutcome).filter_by(
                recommendation_id=buy_id, horizon="1d").one()
            assert buy_1d.actionable is True
            # Raw horizon (close) return is still recorded for research...
            assert abs(buy_1d.return_pct - round((18.20 - 18.00) / 18.00 * 100, 4)) < 1e-3
            # ...but the hedge exits at the target (18.15), which was touched first.
            exp_ret = round((18.15 - 18.00) / 18.00 * 100, 4)         # +0.8333
            assert abs(buy_1d.hedge_return_pct - exp_ret) < 1e-3, buy_1d.hedge_return_pct
            exp_gross = round(100000 * exp_ret / 100, 2)               # ~833.33
            assert abs(buy_1d.gross_pnl_usd - exp_gross) < 0.5, buy_1d.gross_pnl_usd
            assert abs(buy_1d.net_pnl_usd - (exp_gross - 40)) < 0.5, buy_1d.net_pnl_usd
            assert buy_1d.holding_time_hours and buy_1d.time_to_target_hours is not None

            nt_1d = db.query(RecommendationOutcome).filter_by(
                recommendation_id=no_trade_id, horizon="1d").one()
            assert nt_1d.actionable is False
            assert nt_1d.net_pnl_usd is None, "PASS/NO_TRADE must not generate hedge P/L"
        finally:
            db.close()

    def research_summary_and_calibration():
        db = SessionLocal()
        try:
            s = rl.research_summary(db)
            for k in ("overall_accuracy", "accuracy_by_grade", "accuracy_by_regime",
                      "accuracy_by_confidence", "accuracy_by_model_version",
                      "confidence_calibration", "signal_stability", "top_drivers",
                      "weakest_drivers", "provider_reliability", "self_assessment"):
                assert k in s, k
            assert "9.9-test" in s["accuracy_by_model_version"], s["accuracy_by_model_version"]
            cal = rl.calibration(db)["buckets"]
            assert "85-100" in cal, cal
            mp = rl.model_performance(db)["by_model_version"]
            assert "9.9-test" in mp, mp
        finally:
            db.close()

    def paper_hedge_and_monthly_totals():
        db = SessionLocal()
        try:
            ph = rl.paper_hedge_performance(db)
            assert ph["label"] == "SIMULATED PAPER PERFORMANCE"
            assert ph["actionable_trades"] == 1, ph  # only the BUY reco
            assert ph["transaction_costs_usd"] == 40.0, ph
            assert ph["net_pnl_usd"] > 0, ph
            assert ph["win_rate"] == 100.0, ph

            mo = rl.monthly_performance(db)["months"]
            month = (datetime.now(timezone.utc) - timedelta(days=6)).strftime("%Y-%m")
            assert month in mo, (month, list(mo))
            assert mo[month]["total_recommendations"] >= 2, mo[month]
            assert mo[month]["actionable_recommendations"] == 1, mo[month]
        finally:
            db.close()

    def research_endpoints_respond():
        with TestClient(app) as c:
            for ep in ("/research/summary", "/research/calibration", "/research/drivers",
                       "/research/model-performance", "/research/performance",
                       "/performance/monthly", "/performance/summary",
                       "/performance/recommendations"):
                assert c.get(ep).status_code == 200, ep

    check("paper hedge math ($100k notional, $40 cost, actionable only)", paper_hedge_math_is_correct)
    check("research summary + calibration + model performance", research_summary_and_calibration)
    check("paper hedge totals + monthly statistics", paper_hedge_and_monthly_totals)
    check("research + performance endpoints respond", research_endpoints_respond)


def test_evaluator_sparse_data():
    """Verify evaluator behavior against sparse / market-closed price history."""
    from datetime import datetime, timedelta, timezone

    from app.database import SessionLocal, init_db
    from app.models import MarketSnapshot, Recommendation, RecommendationOutcome
    from app.services import recommendation_evaluator as rev

    init_db()

    def _reset(db):
        db.query(RecommendationOutcome).delete()
        db.query(Recommendation).delete()
        db.query(MarketSnapshot).delete()
        db.commit()

    def nearest_after_snapshot_used_when_exact_missing():
        db = SessionLocal()
        try:
            _reset(db)
            now = datetime.now(timezone.utc)
            t0 = (now - timedelta(days=3)).replace(hour=10, minute=0, second=0, microsecond=0)
            reco = Recommendation(pair="USDMXN", created_at=t0, spot_price=18.00,
                                  direction="BUY_USD", confidence=70.0,
                                  opportunity_grade="B", model_version="t",
                                  target=18.50, stop=17.50)
            db.add(reco)
            db.add(MarketSnapshot(pair="USDMXN", usdmxn=18.00, created_at=t0))
            # No snapshot exactly at the 1h due time; nearest one is 40 min later.
            due_1h = rev.horizon_due_time(t0, "1h")
            db.add(MarketSnapshot(pair="USDMXN", usdmxn=18.07,
                                  created_at=due_1h + timedelta(minutes=40)))
            db.commit()
            rev.evaluate_due(db, now=now)
            o = db.query(RecommendationOutcome).filter_by(
                recommendation_id=reco.id, horizon="1h").one()
            assert abs(o.spot_at_evaluation - 18.07) < 1e-6, o.spot_at_evaluation
        finally:
            db.close()

    def pending_when_no_future_price():
        db = SessionLocal()
        try:
            _reset(db)
            now = datetime.now(timezone.utc)
            t0 = (now - timedelta(hours=2)).replace(minute=0, second=0, microsecond=0)
            reco = Recommendation(pair="USDMXN", created_at=t0, spot_price=18.00,
                                  direction="BUY_USD", confidence=70.0,
                                  opportunity_grade="B", model_version="t",
                                  target=18.5, stop=17.5)
            db.add(reco)
            # Only an entry-time price exists; nothing at/after the 1h horizon.
            db.add(MarketSnapshot(pair="USDMXN", usdmxn=18.00, created_at=t0))
            db.commit()
            res = rev.evaluate_due(db, now=now)
            assert res["evaluated"] == 0, res
            db.refresh(reco)
            assert reco.evaluation_status == "pending", reco.evaluation_status
            assert db.query(RecommendationOutcome).count() == 0
        finally:
            db.close()

    def market_closed_horizon_pending_then_uses_next_snapshot():
        db = SessionLocal()
        try:
            _reset(db)
            now = datetime.now(timezone.utc)
            t0 = (now - timedelta(days=4)).replace(hour=10, minute=0, second=0, microsecond=0)
            reco = Recommendation(pair="USDMXN", created_at=t0, spot_price=18.00,
                                  direction="BUY_USD", confidence=70.0,
                                  opportunity_grade="B", model_version="t",
                                  target=18.5, stop=17.5)
            db.add(reco)
            db.add(MarketSnapshot(pair="USDMXN", usdmxn=18.00, created_at=t0))
            db.commit()
            # First pass: no snapshot after the 1h horizon (market closed) -> pending.
            rev.evaluate_due(db, now=now)
            assert db.query(RecommendationOutcome).filter_by(
                recommendation_id=reco.id, horizon="1h").count() == 0

            # Market reopens: a valid next snapshot appears well after the horizon.
            due_1h = rev.horizon_due_time(t0, "1h")
            db.add(MarketSnapshot(pair="USDMXN", usdmxn=18.09,
                                  created_at=due_1h + timedelta(days=2)))
            db.commit()
            rev.evaluate_due(db, now=now)
            o = db.query(RecommendationOutcome).filter_by(
                recommendation_id=reco.id, horizon="1h").one()
            assert abs(o.spot_at_evaluation - 18.09) < 1e-6, o.spot_at_evaluation
        finally:
            db.close()

    def no_duplicate_evaluations():
        db = SessionLocal()
        try:
            _reset(db)
            now = datetime.now(timezone.utc)
            t0 = (now - timedelta(days=6)).replace(hour=10, minute=0, second=0, microsecond=0)
            reco = Recommendation(pair="USDMXN", created_at=t0, spot_price=18.00,
                                  direction="BUY_USD", confidence=70.0,
                                  opportunity_grade="B", model_version="t",
                                  target=18.5, stop=17.5)
            db.add(reco)
            db.add(MarketSnapshot(pair="USDMXN", usdmxn=18.00, created_at=t0))
            for h in rev.HORIZONS:
                db.add(MarketSnapshot(pair="USDMXN", usdmxn=18.10,
                                      created_at=rev.horizon_due_time(t0, h)))
            db.commit()
            first = rev.evaluate_due(db, now=now)
            assert first["evaluated"] == len(rev.HORIZONS), first
            second = rev.evaluate_due(db, now=now)
            assert second["evaluated"] == 0, second  # nothing re-scored
            assert db.query(RecommendationOutcome).filter_by(
                recommendation_id=reco.id).count() == len(rev.HORIZONS)
        finally:
            db.close()

    def hedge_exit_logic_target_stop_or_close():
        db = SessionLocal()
        try:
            _reset(db)
            now = datetime.now(timezone.utc)
            t0 = (now - timedelta(days=6)).replace(hour=10, minute=0, second=0, microsecond=0)

            # BUY where the STOP is touched first (price dips to 17.85 then recovers).
            stop_first = Recommendation(pair="USDMXN", created_at=t0, spot_price=18.00,
                                        direction="BUY_USD", confidence=70.0,
                                        opportunity_grade="B", model_version="t",
                                        target=18.30, stop=17.90)
            # BUY where the TARGET is touched first.
            target_first = Recommendation(pair="USDMXN", created_at=t0, spot_price=18.00,
                                          direction="BUY_USD", confidence=70.0,
                                          opportunity_grade="B", model_version="t",
                                          target=18.10, stop=17.50)
            # BUY where neither target nor stop is touched -> exit at close.
            no_touch = Recommendation(pair="USDMXN", created_at=t0, spot_price=18.00,
                                      direction="BUY_USD", confidence=70.0,
                                      opportunity_grade="B", model_version="t",
                                      target=19.00, stop=17.00)
            db.add_all([stop_first, target_first, no_touch])
            db.add(MarketSnapshot(pair="USDMXN", usdmxn=18.00, created_at=t0))
            # Intra-window path: dip to 17.85 (1h), recover to 18.20 (1d close).
            db.add(MarketSnapshot(pair="USDMXN", usdmxn=17.85,
                                  created_at=rev.horizon_due_time(t0, "1h")))
            db.add(MarketSnapshot(pair="USDMXN", usdmxn=18.20,
                                  created_at=rev.horizon_due_time(t0, "1d")))
            db.commit()
            rev.evaluate_due(db, now=now)

            s = db.query(RecommendationOutcome).filter_by(
                recommendation_id=stop_first.id, horizon="1d").one()
            # Stop (17.90) crossed at the 1h dip -> exit at stop, negative hedge.
            assert abs(s.hedge_return_pct - round((17.90 - 18.00) / 18.00 * 100, 4)) < 1e-3, s.hedge_return_pct
            assert s.net_pnl_usd < 0, s.net_pnl_usd

            tgt = db.query(RecommendationOutcome).filter_by(
                recommendation_id=target_first.id, horizon="1d").one()
            # Target (18.10) reached by the 18.20 close -> exit at target.
            assert abs(tgt.hedge_return_pct - round((18.10 - 18.00) / 18.00 * 100, 4)) < 1e-3, tgt.hedge_return_pct

            nt = db.query(RecommendationOutcome).filter_by(
                recommendation_id=no_touch.id, horizon="1d").one()
            # No level touched -> exit at the nearest evaluation (close) price 18.20.
            assert abs(nt.hedge_return_pct - round((18.20 - 18.00) / 18.00 * 100, 4)) < 1e-3, nt.hedge_return_pct
        finally:
            db.close()

    def no_trade_stored_without_hedge_and_model_version_present():
        db = SessionLocal()
        try:
            _reset(db)
            now = datetime.now(timezone.utc)
            t0 = (now - timedelta(days=2)).replace(hour=10, minute=0, second=0, microsecond=0)
            nt = Recommendation(pair="USDMXN", created_at=t0, spot_price=18.00,
                                direction="NO_TRADE", confidence=40.0,
                                opportunity_grade="PASS", model_version="t")
            db.add(nt)
            db.add(MarketSnapshot(pair="USDMXN", usdmxn=18.00, created_at=t0))
            db.add(MarketSnapshot(pair="USDMXN", usdmxn=18.05,
                                  created_at=rev.horizon_due_time(t0, "1d")))
            db.commit()
            rev.evaluate_due(db, now=now)
            o = db.query(RecommendationOutcome).filter_by(
                recommendation_id=nt.id, horizon="1d").one()
            assert o.actionable is False
            assert o.hedge_return_pct is None and o.gross_pnl_usd is None and o.net_pnl_usd is None
            # model_version is stamped on every stored recommendation.
            assert nt.model_version == "t"
        finally:
            db.close()

    def live_analysis_stamps_model_version():
        with TestClient(app) as c:
            c.get("/analysis/usdmxn")
            r = c.get("/recommendations/recent?limit=1").json()["recommendations"][0]
            assert r.get("model_version"), r

    check("nearest snapshot after horizon used when exact price missing", nearest_after_snapshot_used_when_exact_missing)
    check("no future price -> outcome stays pending", pending_when_no_future_price)
    check("market-closed horizon pending until a valid next snapshot exists", market_closed_horizon_pending_then_uses_next_snapshot)
    check("no duplicate evaluations per recommendation + horizon", no_duplicate_evaluations)
    check("paper hedge exit logic: target / stop first-touch else close", hedge_exit_logic_target_stop_or_close)
    check("NO_TRADE stored for research but no hedge P/L", no_trade_stored_without_hedge_and_model_version_present)
    check("model_version stored on every recommendation", live_analysis_stamps_model_version)


def test_recommendation_history_and_pending():
    """Verify pending-evaluation counts and the recommendation history view."""
    from datetime import datetime, timedelta, timezone

    from app.database import SessionLocal, init_db
    from app.models import MarketSnapshot, Recommendation, RecommendationOutcome
    from app.services import recommendation_evaluator as rev

    init_db()

    def seed():
        db = SessionLocal()
        try:
            db.query(RecommendationOutcome).delete()
            db.query(Recommendation).delete()
            db.query(MarketSnapshot).delete()
            db.commit()

            now = datetime.now(timezone.utc)
            t0 = (now - timedelta(days=6)).replace(hour=10, minute=0, second=0, microsecond=0)

            buy = Recommendation(pair="USDMXN", created_at=t0, spot_price=18.00,
                                 direction="BUY_USD", confidence=82.0,
                                 opportunity_grade="A", model_version="hist-buy",
                                 target=18.10, stretch_target=18.30, stop=17.80)
            no_trade = Recommendation(pair="USDMXN", created_at=t0, spot_price=18.00,
                                      direction="NO_TRADE", confidence=40.0,
                                      opportunity_grade="PASS", model_version="hist-nt")
            # Fresh actionable reco: no horizon is due yet -> fully pending.
            fresh = Recommendation(pair="USDMXN", created_at=now, spot_price=18.00,
                                   direction="SELL_USD", confidence=60.0,
                                   opportunity_grade="C", model_version="hist-fresh",
                                   target=17.80, stop=18.20)
            db.add_all([buy, no_trade, fresh])
            db.add(MarketSnapshot(pair="USDMXN", usdmxn=18.00, created_at=t0))
            # Prices only through the 1d horizon (2d/5d remain pending).
            for h, px in {"1h": 18.05, "4h": 18.08, "end_of_day": 18.09, "1d": 18.20}.items():
                db.add(MarketSnapshot(pair="USDMXN", usdmxn=px,
                                      created_at=rev.horizon_due_time(t0, h)))
            db.commit()
            rev.evaluate_due(db, now=now)
        finally:
            db.close()

    seed()

    def pending_counts_are_correct():
        with TestClient(app) as c:
            p = c.get("/research/pending").json()
            assert p["recommendations_stored"] == 3, p
            assert p["recommendations_evaluated"] == 2, p   # buy + no_trade scored
            assert p["recommendations_pending"] == 1, p     # the fresh reco
            ev, pe = p["evaluated_by_horizon"], p["pending_by_horizon"]
            for h in ("1h", "4h", "end_of_day", "1d"):
                assert ev[h] == 2, (h, ev)
                assert pe[h] == 1, (h, pe)   # 3 stored - 2 scored
            for h in ("2d", "5d"):
                assert ev[h] == 0, (h, ev)
                assert pe[h] == 3, (h, pe)
            # Same block is also surfaced inside the research summary.
            s = c.get("/research/summary").json()
            assert s["evaluation_progress"]["recommendations_stored"] == 3, s["evaluation_progress"]

    def history_rows_have_status_and_pnl():
        with TestClient(app) as c:
            h = c.get("/recommendations/history?limit=50").json()
            assert h["count"] == 3, h
            assert h["horizons"] == list(rev.HORIZONS), h["horizons"]
            rows = {r["model_version"]: r for r in h["recommendations"]}

            buy = rows["hist-buy"]
            assert buy["direction"] == "BUY_USD" and buy["actionable"] is True
            assert buy["horizon_status"]["1h"] == "Win"      # up, target not yet hit
            assert buy["horizon_status"]["1d"] == "Target"   # target 18.10 reached
            assert buy["horizon_status"]["2d"] == "Pending"  # not due / no price
            assert buy["paper_pnl_usd"] is not None          # 1d evaluated

            nt = rows["hist-nt"]
            assert nt["actionable"] is False
            assert nt["paper_pnl_usd"] is None, "NO_TRADE must show N/A paper P/L"
            assert nt["horizon_status"]["1d"] == "N/A"
            assert nt["horizon_status"]["2d"] == "Pending"

            fresh = rows["hist-fresh"]
            assert fresh["actionable"] is True
            assert all(v == "Pending" for v in fresh["horizon_status"].values()), fresh["horizon_status"]
            assert fresh["paper_pnl_usd"] is None             # actionable but pending

    check("pending evaluation counts (stored/evaluated/by-horizon)", pending_counts_are_correct)
    check("recommendation history: per-horizon status + paper P/L", history_rows_have_status_and_pnl)


def test_decision_quality():
    """Verify the Phase 5.3 decision-quality engine."""
    from datetime import datetime, timedelta, timezone

    from app.database import SessionLocal, init_db
    from app.models import Recommendation, RecommendationOutcome
    from app.services import decision_quality as dq

    init_db()

    def pass_no_trade_is_wait():
        db = SessionLocal()
        try:
            rec = {"direction": "NO_TRADE", "grade": "PASS", "trade_score": 20.0,
                   "confidence": 35.0, "entry": None, "target": None, "stop": None,
                   "vix": 15.0, "high_impact_event_count": 0}
            out = dq.assess_recommendation(db, rec)
            assert out["should_trade_now"] is False
            assert out["trade_quality_label"] == "Wait", out["trade_quality_label"]
            assert out["expected_value"]["expected_value_usd"] is None
            assert "NO_TRADE" in (out["reason_to_wait"] or "")
        finally:
            db.close()

    def weak_grade_explains_wait():
        db = SessionLocal()
        try:
            # Grade C, even with a fine R/R, must not trade and must explain why.
            rec = {"direction": "BUY_USD", "grade": "C", "trade_score": 45.0,
                   "confidence": 55.0, "entry": 18.00, "target": 18.20, "stop": 17.90,
                   "vix": 15.0, "prob_target": 50.0, "prob_stop": 40.0,
                   "high_impact_event_count": 0}
            out = dq.assess_recommendation(db, rec)
            assert out["should_trade_now"] is False, out["should_trade_now"]
            assert out["trade_quality_label"] in ("Marginal", "Poor", "Wait")
            assert "below B" in (out["reason_to_wait"] or "")
            assert out["better_entry_conditions"], "should suggest better conditions"
        finally:
            db.close()

    def strong_grade_with_good_rr_can_trade():
        db = SessionLocal()
        try:
            rec = {"direction": "BUY_USD", "grade": "A", "trade_score": 80.0,
                   "confidence": 82.0, "entry": 18.00, "target": 18.20, "stop": 17.90,
                   "vix": 13.0, "best_similarity": 0.82, "hist_win_rate": 66.0,
                   "prob_target": 64.0, "prob_stop": 28.0, "high_impact_event_count": 0,
                   "regime": "Trending"}
            out = dq.assess_recommendation(db, rec)
            assert out["reward_risk"]["reward_risk_ratio"] == 2.0, out["reward_risk"]
            assert out["should_trade_now"] is True, out
            assert out["trade_quality_label"] in ("Good", "Excellent")
            assert out["reason_to_trade"] and out["reason_to_wait"] is None
            assert out["expected_value"]["expected_value_usd"] is not None
        finally:
            db.close()

    def expected_value_deducts_round_trip_cost():
        ev = dq.expected_value("BUY_USD", 18.00, 18.18, 17.90, 60.0, 40.0)
        # Recompute from the raw inputs (avoids rounding artifacts).
        reward_pct = abs(18.18 - 18.00) / 18.00 * 100
        risk_pct = abs(18.00 - 17.90) / 18.00 * 100
        gross_pct = 0.60 * reward_pct - 0.40 * risk_pct
        gross_usd = dq.PAPER_NOTIONAL_USD * gross_pct / 100.0
        assert ev["round_trip_cost_usd"] == 40.0
        assert abs(ev["expected_value_usd"] - round(gross_usd - 40.0, 2)) < 0.01, ev
        # The cost is genuinely subtracted: EV-with-cost is exactly $40 below gross.
        assert abs((gross_usd - ev["expected_value_usd"]) - 40.0) < 0.01, ev
        # Min required win rate (breakeven) is risk/(reward+risk).
        rr = dq.reward_risk("BUY_USD", 18.00, 18.18, 17.90)
        assert rr["minimum_required_win_rate"] is not None and 0 < rr["minimum_required_win_rate"] < 100

    def not_enough_history_is_not_overstated():
        db = SessionLocal()
        try:
            db.query(RecommendationOutcome).delete()
            db.query(Recommendation).delete()
            db.commit()
            tr = dq.similar_track_record(db, "BUY_USD", "A", "Trending")
            assert tr["enough_history"] is False, tr
            assert tr["similar_recommendation_count"] < dq._MIN_SIMILAR, tr
            assert "Not enough similar history yet" in tr["note"], tr["note"]
            # With no history these must not be fabricated.
            assert tr["similar_win_rate"] is None and tr["similar_avg_pnl"] is None, tr
            # ...and the quality blend must not lean on them.
            rec = {"direction": "BUY_USD", "grade": "A", "trade_score": 80.0,
                   "confidence": 82.0, "entry": 18.00, "target": 18.20, "stop": 17.90,
                   "vix": 13.0, "prob_target": 60.0, "prob_stop": 30.0,
                   "high_impact_event_count": 0}
            out = dq.assess_recommendation(db, rec)
            assert out["components"]["model_track_record"] is None, out["components"]
            assert out["components"]["paper_hedge_similar"] is None, out["components"]
        finally:
            db.close()

    def selective_makes_no_claims_at_zero_samples():
        db = SessionLocal()
        try:
            db.query(RecommendationOutcome).delete()
            db.query(Recommendation).delete()
            db.commit()
            s = dq.selective_performance(db)
            assert s["all_trades"]["trades"] == 0, s["all_trades"]
            assert s["all_trades"]["win_rate"] is None, s["all_trades"]
            assert s["all_trades"]["return_on_notional_pct"] is None, s["all_trades"]
            for f in s["filters"].values():
                assert f["trades"] == 0 and f["win_rate"] is None, f
        finally:
            db.close()

    def selective_works_with_limited_history():
        db = SessionLocal()
        try:
            db.query(RecommendationOutcome).delete()
            db.query(Recommendation).delete()
            db.commit()
            base = datetime.now(timezone.utc) - timedelta(days=10)
            seeds = [  # (grade, conf, score, net, win)
                ("A", 90, 88, 600.0, True),
                ("B", 78, 72, 200.0, True),
                ("B", 75, 70, -260.0, False),
                ("C", 60, 50, -120.0, False),
                ("A", 85, 80, 360.0, True),
            ]
            for i, (g, conf, score, net, win) in enumerate(seeds):
                reco = Recommendation(
                    pair="USDMXN", created_at=base + timedelta(hours=i),
                    direction="BUY_USD", confidence=float(conf), opportunity_grade=g,
                    trade_score=float(score), model_version="dq-test",
                    spot_price=18.0, target=18.2, stop=17.9,
                    evaluation_status="complete",
                )
                db.add(reco)
                db.flush()
                db.add(RecommendationOutcome(
                    recommendation_id=reco.id, horizon="1d",
                    direction_correct=win, target_hit=win, stop_hit=(not win),
                    actionable=True, net_pnl_usd=net, gross_pnl_usd=net + 40.0,
                    return_pct=(net / 1000.0),
                ))
            db.commit()

            s = dq.selective_performance(db)
            assert s["all_trades"]["trades"] == 5, s["all_trades"]
            f = s["filters"]
            for key in ("top_10pct", "top_20pct", "top_30pct", "grade_A_or_better",
                        "grade_B_or_better", "confidence_over_70", "confidence_over_80"):
                assert key in f, key
            assert f["top_10pct"]["trades"] == 1, f["top_10pct"]      # 10% of 5 -> 1
            assert f["grade_A_or_better"]["trades"] == 2, f["grade_A_or_better"]
            assert f["grade_B_or_better"]["trades"] == 4, f["grade_B_or_better"]
            assert f["confidence_over_80"]["trades"] == 2, f["confidence_over_80"]
            # Selectivity should improve net P/L vs trading everything.
            assert f["grade_A_or_better"]["net_pnl_usd"] >= s["all_trades"]["net_pnl_usd"]
            assert s["all_trades"]["max_drawdown_usd"] >= 0.0
        finally:
            db.close()

    def endpoints_and_payload_integration():
        with TestClient(app) as c:
            body = c.get("/analysis/usdmxn").json()
            assert "decision_quality" in body, list(body)[:20]
            dqp = body["decision_quality"]
            for k in ("trade_quality_score", "trade_quality_label", "should_trade_now",
                      "reward_risk", "expected_value", "similar_track_record"):
                assert k in dqp, k
            q = c.get("/decision/quality").json()
            assert q.get("available") is True and "trade_quality_label" in q, q
            # /decision/quality must agree with the latest /analysis recommendation.
            assert q["direction"] == body["direction"], (q["direction"], body["direction"])
            assert q["opportunity_grade"] == body["opportunity_grade"], q
            assert q["should_trade_now"] == dqp["should_trade_now"], (q, dqp)
            assert q["trade_quality_label"] == dqp["trade_quality_label"], (q, dqp)
            cc = c.get("/decision/current-context").json()
            assert cc.get("available") is True and "similar_track_record" in cc, cc
            sp = c.get("/decision/selective-performance").json()
            assert "filters" in sp and "all_trades" in sp, sp

    check("PASS / NO_TRADE -> should_trade_now=false, label Wait", pass_no_trade_is_wait)
    check("weak C grade explains why to wait", weak_grade_explains_wait)
    check("B/A/A+ with supporting R/R -> should_trade_now=true", strong_grade_with_good_rr_can_trade)
    check("expected value deducts $40 round-trip cost", expected_value_deducts_round_trip_cost)
    check("not-enough similar history is not overstated", not_enough_history_is_not_overstated)
    check("selective analysis makes no claims at zero samples", selective_makes_no_claims_at_zero_samples)
    check("selective trading analysis works with limited history", selective_works_with_limited_history)
    check("decision endpoints + analysis payload integration (agreement)", endpoints_and_payload_integration)


def test_provenance_engine():
    """Verify the Phase 5.4 evidence & provenance engine."""
    from app.services import provenance as pv

    def _payload(market_source="live", field_sources=None, historical="sample"):
        return {
            "direction": "BUY_USD", "confidence": 70.0, "entry": 18.00,
            "target": 18.20, "stretch_target": 18.30, "stop": 17.90,
            "expected_move": "+0.5%",
            "market": {
                "usdmxn": 18.00, "dxy": 104.0, "vix": 15.0, "provider": "oxr",
                "source": market_source,
                "sources": field_sources or {"usdmxn": market_source, "dxy": "live", "vix": "mock"},
                "created_at": "2026-06-27T12:00:00+00:00",
            },
            "data_sources": {"market": market_source, "historical": historical},
            "historical": {"best_similarity": 0.80, "statistics": {"win_rate": 60.0}},
            "probabilities": {"levels": {"probability_reaches_target_1": 62.0}},
            "decision_quality": {
                "trade_quality_score": 70.0,
                "expected_value": {"expected_value_usd": 120.0},
                "similar_track_record": {"enough_history": False, "similar_win_rate": None,
                                         "note": "Not enough similar history yet — 0."},
            },
        }

    def levels_map_correctly():
        # Live market data -> LIVE (3); a mock field -> SAMPLE (0).
        prov = pv.build(_payload(), {"cached": False, "provider": "oxr",
                                     "fetched_at": "2026-06-27T12:00:00+00:00"})
        assert prov["spot_rate"]["source"] == "live" and prov["spot_rate"]["evidence_level"] == 3
        assert prov["dxy"]["source"] == "live"
        assert prov["vix"]["source"] == "sample" and prov["vix"]["evidence_level"] == 0
        # Trade plan is an estimate by default.
        assert prov["target"]["source"] == "estimated" and prov["target"]["evidence_level"] == 1
        # Cached market overlay downgrades live -> cached (2).
        prov_c = pv.build(_payload(), {"cached": True, "provider": "oxr"})
        assert prov_c["spot_rate"]["source"] == "cached" and prov_c["spot_rate"]["evidence_level"] == 2

    def every_major_metric_has_provenance():
        prov = pv.build(_payload(), {"cached": False})
        required = ("spot_rate", "entry", "target", "stretch_target", "stop",
                    "expected_move", "probabilities", "confidence",
                    "historical_similarity", "historical_win_rate",
                    "recommendation_accuracy", "trade_quality_score",
                    "expected_value", "similar_track_record")
        for f in required:
            assert f in prov, f
            for key in ("value", "source", "evidence_level", "badge", "explanation"):
                assert key in prov[f], (f, key)
            assert prov[f]["source"] in pv.LEVELS, prov[f]

    def measured_and_estimated_stay_separate():
        # No measured history: recommendation accuracy is NOT measured.
        prov = pv.build(_payload(), {"cached": False},
                        measured_available=False, similar_measured=False)
        assert prov["recommendation_accuracy"]["source"] == "estimated"
        assert prov["recommendation_accuracy"]["value"] is None
        # Historical similarity is never "measured" — it's historical/sample.
        assert prov["historical_similarity"]["source"] == "sample"
        assert prov["historical_similarity"]["label"] == "Sample Historical Database"
        # With measured outcomes, recommendation accuracy becomes measured (L5).
        prov_m = pv.build(_payload(), {"cached": False},
                          measured_available=True, measured_accuracy=61.0)
        assert prov_m["recommendation_accuracy"]["source"] == "measured"
        assert prov_m["recommendation_accuracy"]["evidence_level"] == 5
        assert prov_m["recommendation_accuracy"]["value"] == 61.0
        # ...but the similarity score stays historical/sample (not conflated).
        assert prov_m["historical_similarity"]["source"] != "measured"

    def historical_db_label_tracks_source():
        s = pv.build(_payload(historical="sample"), {"cached": False})
        assert s["historical_similarity"]["label"] == "Sample Historical Database"
        b = pv.build(_payload(historical="backfilled"), {"cached": False})
        assert b["historical_similarity"]["label"] == "Historical Database"
        assert b["historical_similarity"]["source"] == "historical"

    def estimated_upgrades_to_measured_when_history_supports():
        base = pv.build(_payload(), {"cached": False}, similar_measured=False)
        assert base["target"]["source"] == "estimated"
        # Same value, only provenance changes once measured similar history exists.
        up = pv.build(_payload(), {"cached": False}, similar_measured=True)
        assert up["target"]["source"] == "measured", up["target"]
        assert up["target"]["value"] == base["target"]["value"]  # value unchanged
        assert up["probabilities"]["source"] == "measured"

    def overview_summarizes_counts():
        prov = pv.build(_payload(), {"cached": False})
        ov = pv.overview(prov)
        assert ov["total_metrics"] == len(prov)
        assert sum(ov["counts"].values()) == len(prov)
        assert ov["order"] == pv.SOURCE_ORDER
        for s in pv.SOURCE_ORDER:
            assert s in ov["by_source"] and "fields" in ov["by_source"][s]
        assert 0.0 <= ov["evidence_backed_share_pct"] <= 100.0

    def api_returns_provenance():
        with TestClient(app) as c:
            d = c.get("/analysis/usdmxn").json()
            assert "provenance" in d and "evidence_overview" in d, list(d)[:25]
            prov = d["provenance"]
            assert "spot_rate" in prov and "evidence_level" in prov["spot_rate"], prov.get("spot_rate")
            ov = d["evidence_overview"]
            assert ov["total_metrics"] == sum(ov["counts"].values()), ov

    check("evidence levels map correctly (live/cached/sample/estimated)", levels_map_correctly)
    check("every major metric carries provenance", every_major_metric_has_provenance)
    check("measured and estimated statistics stay separate", measured_and_estimated_stay_separate)
    check("historical database label tracks the source", historical_db_label_tracks_source)
    check("estimated upgrades to measured when history supports (value unchanged)", estimated_upgrades_to_measured_when_history_supports)
    check("evidence summary overview counts add up", overview_summarizes_counts)
    check("API returns provenance + evidence overview", api_returns_provenance)


def test_live_providers():
    def finnhub_filters_and_tags_live():
        # general + forex categories both return this list (deduped by headline).
        payload = [
            {"headline": "Fed's Powell signals fewer rate cuts as inflation lingers",
             "summary": "Treasury yields rose.", "source": "Reuters",
             "url": "https://x/fed", "datetime": 1750000000},
            {"headline": "Banxico holds rate; peso steadies",
             "summary": "Mexico central bank decision.", "source": "Bloomberg",
             "url": "https://x/banxico", "datetime": 1750000100},
            {"headline": "Apple unveils new iPhone lineup",
             "summary": "Consumer tech launch.", "source": "TechCrunch",
             "url": "https://x/apple", "datetime": 1750000200},
        ]
        original = news_svc.httpx.get
        news_svc.httpx.get = lambda *a, **k: _FakeResponse(payload)
        try:
            prov = news_svc.get_news_provider(
                Settings(use_mock_data=False, news_api_key="k", news_provider="finnhub")
            )
            items = prov.get_news()
            assert prov.source == "live", prov.source
            heads = [i["headline"] for i in items]
            assert any("Powell" in h for h in heads), heads
            assert not any("iPhone" in h for h in heads), "unrelated news must be dropped"
            assert all(i["relevance_score"] > 0 for i in items), items
            # ranked by relevance (descending)
            scores = [i["relevance_score"] for i in items]
            assert scores == sorted(scores, reverse=True), scores
        finally:
            news_svc.httpx.get = original

    def macro_per_field_live_and_fallback():
        macro_svc.clear_macro_cache()

        def fake_get(url, *a, **k):
            params = k.get("params", {})
            if "stlouisfed" in url:
                sid = params.get("series_id")
                val = "4.71" if sid == "DGS2" else "4.29"
                return _FakeResponse({"observations": [{"date": "2026-06-26", "value": val}]})
            if "alphavantage" in url:
                fn = params.get("function")
                if fn == "WTI":
                    return _FakeResponse({"data": [{"date": "2026-06-26", "value": "77.2"}]})
                if fn == "CURRENCY_EXCHANGE_RATE":
                    return _FakeResponse({"Realtime Currency Exchange Rate": {"5. Exchange Rate": "2380.5"}})
                # GLOBAL_QUOTE (DXY/VIX/SPX) -> unavailable on the free tier.
                return _FakeResponse({"Global Quote": {}})
            return _FakeResponse({"rates": {"MXN": 18.55}})  # FX spot

        orig_macro, orig_mkt = macro_svc.httpx.get, market_data.httpx.get
        macro_svc.httpx.get = fake_get
        market_data.httpx.get = fake_get
        try:
            md = market_data.get_market_data(Settings(
                use_mock_data=False, fx_api_key="fx",
                fred_api_key="fred", alpha_vantage_api_key="av",
                macro_cache_seconds=0,
            ))
            fs = md.field_sources
            assert md.source == "live" and fs["usdmxn"] == "live", fs
            assert fs["us2y"] == "live" and fs["us10y"] == "live", fs
            assert fs["oil"] == "live" and fs["gold"] == "live", fs
            # No clean free-tier symbol -> retained as fallback, not live.
            assert fs["dxy"] == "fallback" and fs["vix"] == "fallback", fs
            assert fs["sp_futures"] == "fallback", fs
            assert md.us2y == 4.71 and md.us10y == 4.29 and md.oil == 77.2, md
        finally:
            macro_svc.httpx.get, market_data.httpx.get = orig_macro, orig_mkt
            macro_svc.clear_macro_cache()

    def macro_all_mock_without_keys():
        md = market_data.get_market_data(Settings(use_mock_data=True))
        assert md.source == "mock", md.source
        assert all(v == "mock" for v in md.field_sources.values()), md.field_sources

    def macro_scrub_hides_keys():
        s = Settings(fred_api_key="FREDSECRET", alpha_vantage_api_key="AVSECRET")
        msg = macro_svc._scrub("boom FREDSECRET and AVSECRET leaked", s)
        assert "FREDSECRET" not in msg and "AVSECRET" not in msg, msg

    check("Finnhub filters unrelated news + tags live", finnhub_filters_and_tags_live)
    check("macro per-field live + fallback (FRED/Alpha Vantage)", macro_per_field_live_and_fallback)
    check("macro all-mock without keys", macro_all_mock_without_keys)
    check("macro errors never expose API keys", macro_scrub_hides_keys)


def test_evidence_engine():
    from app.services.history import historical_statistics as hs
    from app.services.history.importers import MockSampleImporter

    def synthetic_library_is_large():
        # The pattern library must be deep enough for top-25 nearest neighbors.
        events = MockSampleImporter().fetch_events()
        assert len(events) >= 50, len(events)

    def wilson_interval_bounds():
        ci = hs.wilson_interval(8, 10)
        assert ci is not None and len(ci) == 2
        lo, hi = ci
        assert 0.0 <= lo <= 80.0 <= hi <= 100.0, ci
        assert hs.wilson_interval(0, 0) is None

    def expanded_outcome_stats():
        matches = [
            {"windows": {"1d": 0.5, "3d": 0.4, "5d": 0.3}, "similarity_score": 0.9,
             "max_favorable_excursion": 0.7, "max_adverse_excursion": 0.2,
             "time_to_peak_hours": 20.0, "reversal_behavior": "continuation"},
            {"windows": {"1d": -0.2, "3d": 0.1, "5d": -0.3}, "similarity_score": 0.6,
             "max_favorable_excursion": 0.3, "max_adverse_excursion": 0.5,
             "time_to_peak_hours": 40.0, "reversal_behavior": "reversal"},
        ]
        stats = hs.aggregate_statistics(matches, direction="BUY_USD", current_price=18.0)
        assert stats["best_move"] == 0.5 and stats["worst_move"] == -0.2, stats
        assert stats["max_drawdown"] == 0.5, stats
        assert stats["reversal_probability"] == 50.0, stats
        assert stats["average_MFE"] is not None and stats["average_MAE"] is not None
        assert stats["wins"] == 1, stats

    def evidence_probabilities_have_detail():
        matches = [
            {"windows": {"1h": 0.2, "1d": 0.5, "3d": 0.6, "5d": 0.45}},
            {"windows": {"1h": 0.3, "1d": 0.9, "3d": 0.4, "5d": 0.8}},
        ]
        targets = {"target_1": 100.5, "target_2": None, "stretch": 101.0, "stop": 99.6}
        out = hs.probability_forecast(matches, 100.0, "BUY_USD", targets)
        ev = out["evidence"]
        rt = ev["reaches_target"]
        assert rt["value"] == 100.0 and rt["sample_size"] == 2, rt
        assert rt["confidence_interval"] and rt["basis"], rt
        assert "finishes_positive_today" in ev and ev["finishes_positive_today"]
        assert "probability_finishes_positive_today" in out["levels"]

    def setup_percentile_ranks():
        ref = [-1.0, -0.5, 0.0, 0.5, 1.0]
        assert hs.setup_percentile(ref, 1.0) == 100.0
        assert hs.setup_percentile(ref, -1.0) == 20.0
        assert hs.setup_percentile([], 0.5) is None

    def confidence_explained():
        out = hs.blend_confidence({"signal": 80, "historical": 60, "regime": 70,
                                   "volatility": 90, "data_quality": 50})
        assert isinstance(out["explanation"], list) and out["explanation"]
        assert "weighted blend" in out["formula"], out["formula"]
        assert any("Current signals" in line for line in out["explanation"])

    def narrative_reads_like_evidence():
        stats = {"sample_size": 143, "wins": 108, "win_rate": 75.5,
                 "average_move": 0.46, "median_move": 0.39,
                 "average_holding_hours": 31.0, "max_drawdown": 0.18}
        text = hs.evidence_narrative(stats, "BUY_USD", percentile=91.0)
        assert text and "occurred 143 times" in text, text
        assert "91" in text and "percentile" in text, text
        assert hs.evidence_narrative({"sample_size": 0}, "BUY_USD") is None

    check("evidence: synthetic library is large enough", synthetic_library_is_large)
    check("evidence: Wilson interval bounds", wilson_interval_bounds)
    check("evidence: expanded outcome statistics", expanded_outcome_stats)
    check("evidence: probabilities carry sample/CI/basis", evidence_probabilities_have_detail)
    check("evidence: setup percentile ranking", setup_percentile_ranks)
    check("evidence: confidence breakdown is explained", confidence_explained)
    check("evidence: historical narrative reads like evidence", narrative_reads_like_evidence)


def test_historical_backfill():
    """Phase 5.5: real historical backfill infrastructure."""
    import os
    import tempfile

    from app.database import SessionLocal, init_db
    from app.models import (
        HistoricalEvent,
        HistoricalEventReaction,
        HistoricalMarketSnapshot,
        SimilarityMatch,
    )
    from app.services.history import history_diagnostics
    from app.services.history.historical_events import ensure_history_seeded, load_reactions
    from app.services.history.importers import CSVImporter, get_importer

    init_db()

    def _wipe(db):
        for model in (SimilarityMatch, HistoricalEventReaction,
                      HistoricalMarketSnapshot, HistoricalEvent):
            db.query(model).delete()
        db.commit()

    def mock_is_the_default():
        from app.config import Settings
        assert Settings().history_importer == "mock"
        # Registry resolves real classes; unknown -> mock.
        assert get_importer("alphavantage").name == "alphavantage"
        assert get_importer("fred").name == "fred"
        assert get_importer("csv").name == "csv"
        assert get_importer("does-not-exist").name == "mock"

    def lazy_seed_uses_mock_and_diagnostics_report_sample():
        db = SessionLocal()
        try:
            _wipe(db)
            seeded = ensure_history_seeded(db)
            assert seeded.get("seeded") is True, seeded
            diag = history_diagnostics(db)
            assert diag["active_importer"] == "mock"
            assert diag["counts"]["historical_events"] > 0
            assert diag["counts"]["historical_event_reactions"] > 0
            assert diag["data_class"] == "sample", diag
            assert diag["is_sample_only"] is True
            assert any("SAMPLE" in w for w in diag["warnings"]), diag["warnings"]
            assert diag["last_imported"] is not None
        finally:
            db.close()

    def diagnostics_endpoint_works():
        with TestClient(app) as c:
            r = c.get("/history/diagnostics")
            assert r.status_code == 200, r.status_code
            body = r.json()
            for key in ("active_importer", "counts", "data_class",
                        "similarity_uses", "warnings"):
                assert key in body, (key, list(body))
            assert set(body["counts"]) == {
                "historical_events", "historical_event_reactions",
                "historical_market_snapshots", "similarity_matches",
            }, body["counts"]

    def csv_importer_loads_fixtures_and_takes_priority():
        with tempfile.TemporaryDirectory() as d:
            with open(os.path.join(d, "events.csv"), "w", encoding="utf-8") as fh:
                fh.write(
                    "event_key,event_type,event_name,country,release_time,forecast,actual,"
                    "previous,importance,currency_impact,baseline,dxy,us2y,us10y,oil,gold,"
                    "vix,sp_futures,momentum,regime,news_tags\n"
                    "e1,us_cpi,US CPI (MoM),US,2024-06-12T12:30:00,0.1,0.3,0.2,high,USD,"
                    "18.30,104.0,4.7,4.3,78.0,2330.0,13.0,5400.0,0.02,Inflation Driven,inflation|cpi\n"
                    "e2,us_nfp,US Nonfarm Payrolls,US,2024-07-05T12:30:00,190,206,218,high,USD,"
                    "18.10,105.0,4.6,4.2,83.0,2390.0,12.5,5500.0,0.01,Trending,jobs|nfp\n"
                )
            with open(os.path.join(d, "paths.csv"), "w", encoding="utf-8") as fh:
                fh.write(
                    "event_key,hours,price\n"
                    "e1,0,18.30\ne1,1,18.36\ne1,24,18.45\ne1,120,18.52\n"
                    "e2,0,18.10\ne2,1,18.05\ne2,24,17.98\ne2,120,17.90\n"
                )
            with open(os.path.join(d, "series.csv"), "w", encoding="utf-8") as fh:
                fh.write(
                    "series,ts,value\n"
                    "USDMXN,2024-06-01T00:00:00,18.40\n"
                    "US2Y,2024-06-01T00:00:00,4.72\n"
                    "DXY,2024-06-01T00:00:00,104.3\n"
                )
            db = SessionLocal()
            try:
                _wipe(db)
                # Seed mock first, then import CSV — imported must take priority.
                ensure_history_seeded(db)
                assert history_diagnostics(db)["data_class"] == "sample"

                result = CSVImporter(directory=d).run_all(db)
                assert result["events"] == 2, result
                assert result["reactions"] == 2, result
                assert result["series_points"] == 3, result
                assert not result["errors"], result["errors"]

                # series.csv routed scalar values into the right columns.
                us2y_row = (
                    db.query(HistoricalMarketSnapshot)
                    .filter(HistoricalMarketSnapshot.series == "US2Y").first()
                )
                assert us2y_row is not None and us2y_row.us2y == 4.72
                assert us2y_row.usdmxn is None, "scalar leaked into usdmxn column"

                # Imported reactions now exist -> load_reactions excludes sample.
                reactions = load_reactions(db)
                assert reactions, "expected imported reactions"
                qualities = {r["source_quality"] for r in reactions}
                assert qualities == {"imported"}, qualities

                diag = history_diagnostics(db)
                assert diag["data_class"] == "imported", diag
                assert diag["reactions_data_class"] == "imported"
                assert diag["is_sample_only"] is False
            finally:
                db.close()

    def network_importers_never_leak_keys():
        # Force httpx to raise with the key embedded; the importer must scrub it.
        import httpx as _httpx

        from app.config import Settings
        from app.services.history import importers as imp

        secret = "SUPERSECRETKEY123"

        def boom(*a, **k):
            raise RuntimeError(f"connection failed for apikey={secret}")

        # AlphaVantage
        av = imp.AlphaVantageImporter(
            Settings(alpha_vantage_api_key=secret), throttle=False
        )
        _httpx_get = _httpx.get
        try:
            _httpx.get = boom
            try:
                av._get({"function": "FX_DAILY"})
                raised = False
            except RuntimeError as exc:
                raised = True
                assert secret not in str(exc), "Alpha Vantage leaked the API key!"
                assert "REDACTED" in str(exc)
            assert raised
            # FRED
            fr = imp.FREDImporter(Settings(fred_api_key=secret))
            try:
                fr._observations("DGS2")
                raised = False
            except RuntimeError as exc:
                raised = True
                assert secret not in str(exc), "FRED leaked the API key!"
                assert "REDACTED" in str(exc)
            assert raised
        finally:
            _httpx.get = _httpx_get

    def network_importers_are_series_only_and_not_lazy():
        from app.services.history.importers import AlphaVantageImporter, FREDImporter
        for cls in (AlphaVantageImporter, FREDImporter):
            assert cls.provides_events is False
            assert cls.provides_series is True
            assert cls.lazy_safe is False
        # No key configured -> clear error, no crash, no leak.
        try:
            AlphaVantageImporter(Settings(alpha_vantage_api_key=None))._get({"function": "X"})
            assert False, "expected missing-key error"
        except RuntimeError as exc:
            assert "not configured" in str(exc)

    check("mock remains the default importer", mock_is_the_default)
    check("lazy seed uses mock; diagnostics report SAMPLE", lazy_seed_uses_mock_and_diagnostics_report_sample)
    check("GET /history/diagnostics works", diagnostics_endpoint_works)
    check("CSV importer loads fixtures; imported takes priority over mock", csv_importer_loads_fixtures_and_takes_priority)
    check("network importers never log/leak API keys", network_importers_never_leak_keys)
    check("network importers are series-only and not lazy", network_importers_are_series_only_and_not_lazy)


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
    test_signal_weighting()
    test_reasoning_engine()
    test_strategist_narrative()
    test_multi_horizon()
    test_history_engine()
    test_evidence_engine()
    test_source_labeling()
    test_live_providers()
    test_market_infrastructure()
    test_recommendation_tracking()
    test_research_lab()
    test_evaluator_sparse_data()
    test_recommendation_history_and_pending()
    test_decision_quality()
    test_provenance_engine()
    test_historical_backfill()
    test_scrub()
    print(f"\n{_passed} passed, {_failed} failed")
    sys.exit(1 if _failed else 0)


if __name__ == "__main__":
    main()
