"""20-minute timing, direction, cost and leakage regressions (synthetic fixtures)."""

from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.models import MarketSnapshot
from app.services import short_term_forecast as st

NOW = datetime(2026, 11, 16, 16, 0, tzinfo=timezone.utc)


def fixture(slope=.0008, spread=.0002):
    points = [(NOW - timedelta(minutes=5000 - m), 20 + slope * (m - 5000)) for m in range(0, 5001, 5)]
    fix = {"bid": 20 - spread/2, "ask": 20 + spread/2, "updated_at": NOW.isoformat()}
    payload = {"market_state": {"is_open": True}, "context": {"upcoming_events": []}, "data_sources": {"calendar": "live", "calendar_status": "LIVE"}}
    payload["news_watch"] = {"status": "LIVE_POLL", "checked_at": NOW.isoformat(), "items": []}
    return points, fix, payload


@pytest.mark.parametrize("slope,action,sign", [(.0008, "BUY USD / SELL MXN NOW", 1), (-.0008, "SELL USD / BUY MXN NOW", -1)])
def test_independent_twenty_minute_direction_and_move(slope, action, sign):
    out = st.build(*fixture(slope), now=NOW)
    assert out["action"] == action
    assert out["horizon_minutes"] == 20
    assert out["expected_move_mxn"] * sign > 0
    assert out["expected_move_cents"] == pytest.approx(out["expected_move_mxn"] * 100, abs=.002)
    assert out["validation"]["samples"] >= st.MIN_VALIDATION
    assert out["validation"]["status"] == "walk_forward_simulation"
    assert out["net_pnl_usd"] > 0
    assert "confidence" not in out


@pytest.mark.parametrize("change", [{"updated_at": (NOW-timedelta(seconds=31)).isoformat()}, {"updated_at": (NOW+timedelta(seconds=5)).isoformat()}, {"bid": float("nan")}, {"ask": float("inf")}, {"bid": 21, "ask": 20}, {"updated_at": None}])
def test_invalid_or_stale_fix_cannot_produce_signal(change):
    points, fix, payload = fixture()
    fix.update(change)
    out = st.build(points, fix, payload, now=NOW)
    assert out["action"] == "WAIT"
    assert out["expected_rate"] is None


def test_hourly_forecast_cannot_be_scaled_into_twenty_minutes():
    points, fix, payload = fixture()
    payload["topline_forecast"] = {"horizons": [{"horizon": "1 hour", "expected_rate": 30}]}
    out = st.build(points[::12], fix, payload, now=NOW)
    assert out["action"] == "WAIT"
    assert out["expected_rate"] is None


def test_spread_can_change_buy_forecast_into_wait():
    out = st.build(*fixture(spread=.03), now=NOW)
    assert out["direction"] == "BUY_USD"
    assert out["expected_rate"] > out["spot"]
    assert out["net_pnl_usd"] < 0
    assert out["action"] == "WAIT"


@pytest.mark.parametrize("reason", ["closed", "mock_calendar", "partial_calendar", "coverage_gap", "event", "invalidated"])
def test_existing_risk_restrictions_block_action(reason):
    points, fix, payload = fixture()
    if reason == "closed":
        payload["market_state"]["is_open"] = False
    elif reason == "mock_calendar":
        payload["data_sources"]["calendar"] = "mock"
    elif reason == "partial_calendar":
        payload["data_sources"]["calendar_status"] = "PARTIAL"
    elif reason == "coverage_gap":
        payload["calendar_coverage_gaps"] = ["Mexico data unavailable"]
    elif reason == "invalidated":
        payload["invalidated"] = True
    else:
        payload["context"]["upcoming_events"] = [{"importance": "high", "title": "CPI", "release_time": (datetime.now(timezone.utc)+timedelta(minutes=10)).isoformat()}]
    assert st.build(points, fix, payload, now=NOW)["action"] == "WAIT"


def test_repeated_requests_do_not_inflate_history_sample_size():
    points, fix, payload = fixture()
    original = st.build(points, fix, payload, now=NOW)
    duplicate = st.build(points * 10, fix, payload, now=NOW)
    assert original == duplicate
    examples = st._examples(points)
    assert all(b["at"]-timedelta(minutes=15) > a["end"] for a, b in zip(examples, examples[1:]))


def test_future_points_do_not_leak_into_forecast():
    points, fix, payload = fixture()
    original = st.build(points, fix, payload, now=NOW)
    future = points + [(NOW+timedelta(minutes=i), 500) for i in range(1, 120)]
    assert st.build(future, fix, payload, now=NOW) == original


def test_walk_forward_training_is_purged_of_test_window():
    points, fix, _ = fixture()
    examples = st._examples(points)
    calls = []
    original = st._neighbors
    def capture(training, features):
        calls.append(training)
        return original(training, features)
    start = max(st.MIN_ANALOGS, len(examples)-120)
    with patch.object(st, "_neighbors", side_effect=capture):
        st._validation(examples, "BUY_USD", fix)
    for training, test in zip(calls, examples[start:]):
        assert all(row["end"] < test["at"]-timedelta(minutes=15) for row in training)


def test_database_adapter_excludes_mock_and_hourly_fallback_quotes():
    engine = create_engine("sqlite://")
    MarketSnapshot.__table__.create(engine)
    with Session(engine) as db:
        now = datetime.now(timezone.utc)
        for provider, source in [("centroid_fix", "live"), ("mock", "mock"), ("openexchangerates", "live")]:
            db.add(MarketSnapshot(created_at=now, pair="USDMXN", usdmxn=20, provider=provider, source=source))
        db.commit()
        with patch("app.services.fix.provider.get_fix_quote", return_value=None), patch.object(st, "build", return_value={}) as builder, patch.object(db, "execute", wraps=db.execute) as execute:
            st.from_database(db, {})
            assert len(builder.call_args.args[0]) == 1
            db.add(MarketSnapshot(created_at=now-timedelta(minutes=5), pair="USDMXN", usdmxn=20.01, provider="centroid_fix", source="live"))
            db.commit()
            st.from_database(db, {})
            assert len(builder.call_args.args[0]) == 2
            # History once, a fresh small tail on both requests.
            assert execute.call_count == 3


def test_weak_validation_cannot_borrow_four_hour_confidence():
    points, fix, payload = fixture()
    payload["confidence"] = 99
    payload["opportunity_grade"] = "A+"
    with patch.object(st, "_validation", return_value={"samples": 3, "accuracy_lower_95_pct": 99, "average_net_pnl_usd": 100}):
        out = st.build(points, fix, payload, now=NOW)
    assert out["action"] == "WAIT"
    assert out["expected_rate"] is not None


def test_calendar_failure_after_provider_initialization_is_not_treated_as_live():
    from app.routers.decision import _short_term_calendar
    class Provider:
        source = "live"
        status = "LIVE"
        coverage_gaps = []
        def get_upcoming(self):
            self.source, self.status = "error", "ERROR"
            return []
    _short_term_calendar.cache_clear()
    try:
        with patch("app.services.calendar.get_calendar_provider", return_value=Provider()):
            result = _short_term_calendar(1)
        assert result["data_sources"]["calendar"] == "error"
        assert result["data_sources"]["calendar_status"] == "ERROR"
    finally:
        _short_term_calendar.cache_clear()


def test_dashboard_expires_signals_and_clears_values_after_fetch_failure(tmp_path):
    import subprocess
    from app.main import DASHBOARD_HTML
    start = DASHBOARD_HTML.index("let shortTermExpires")
    end = DASHBOARD_HTML.index("function renderTradeDecisionCard", start)
    js = DASHBOARD_HTML[start:end]
    harness = """
const elements = {};
const $ = id => elements[id] || (elements[id] = {textContent:''});
const setText = (id, value) => $(id).textContent = String(value);
const tlRate4 = v => v == null ? '—' : Number(v).toFixed(4);
const fmtTime = v => v;
let expiryCheck;
const setInterval = callback => { expiryCheck = callback; };
let fetchJson = async () => { throw new Error('feed down'); };
""" + js + """
(async () => {
  renderShortTerm({action:'BUY USD / SELL MXN NOW', expires_at:new Date(Date.now()+60000).toISOString(),
    expected_rate:20.02, spot:20, expected_move_mxn:.02, expected_move_cents:2, net_pnl_usd:40});
  if ($('st_action').textContent !== 'BUY USD / SELL MXN NOW') throw new Error('missing action');
  shortTermExpires = Date.now()-1;
  expiryCheck();
  if ($('st_action').textContent !== 'WAIT' || $('st_target').textContent !== '—') throw new Error('expired action visible');
  renderShortTerm({action:'SELL USD / BUY MXN NOW', expires_at:new Date(Date.now()+60000).toISOString(), expected_rate:19.98});
  await loadShortTerm();
  if ($('st_action').textContent !== 'WAIT' || $('st_pnl').textContent !== '—') throw new Error('failed refresh retained signal');
})().catch(e => { console.error(e); process.exitCode=1; });
"""
    path = tmp_path / "short_term_dashboard.js"
    path.write_text(harness)
    proc = subprocess.run(["node", str(path)], capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr
