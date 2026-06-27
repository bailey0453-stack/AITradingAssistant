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
            assert ds["news"] in {"mock", "live", "fallback"}, ds
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
    test_scrub()
    print(f"\n{_passed} passed, {_failed} failed")
    sys.exit(1 if _failed else 0)


if __name__ == "__main__":
    main()
