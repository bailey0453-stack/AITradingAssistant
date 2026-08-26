"""Per-horizon Recommendation History: directional status + FIX $100k hedge P/L."""

from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

from app.routers.recommendations import serialize_history_row
from app.services.hedge_pnl import (
    ROUND_TRIP_FEES_USD,
    hedge_pnl_detail,
    historical_horizon_pnl,
    net_hedge_pnl_usd,
    reanchor_to_fix,
    stored_fix_quote,
)

# Model spot 18.00 vs FIX mid 16.91 — a large feed-basis gap. P/L must use the
# FIX midpoint + the *model move*, never mix model spot with FIX entry.
FIX = {"bid": 16.90, "ask": 16.92}  # mid 16.91, half-spread 0.01
MODEL_SPOT = 18.00


def _expected_net(direction: str, eval_spot: float) -> float:
    exit_mid = reanchor_to_fix(eval_spot, MODEL_SPOT, FIX)
    detail = hedge_pnl_detail(direction, exit_mid, FIX)
    assert detail is not None
    return detail["net_pnl_usd"]


def _outcome(**kwargs):
    defaults = dict(
        horizon="1h",
        spot_at_evaluation=17.90,
        direction_correct=True,
        target_hit=False,
        stop_hit=False,
        time_to_target_hours=None,
        time_to_stop_hours=None,
        net_pnl_usd=100.0,
    )
    defaults.update(kwargs)
    return SimpleNamespace(**defaults)


def _reco(**kwargs):
    now = datetime(2026, 8, 1, 12, 0, tzinfo=timezone.utc)
    defaults = dict(
        created_at=now,
        recommendation_uuid="reco-test",
        model_version="hist-pnl",
        direction="SELL_USD",
        opportunity_grade="B",
        confidence=70.0,
        trade_score=65.0,
        spot_price=MODEL_SPOT,
        target=17.70,
        stop=18.20,
        evaluation_status="partial",
        outcomes=[],
        fix_bid=FIX["bid"],
        fix_ask=FIX["ask"],
        primary_trade_plan=None,
        strategist=None,
    )
    defaults.update(kwargs)
    return SimpleNamespace(**defaults)


def test_round_trip_fees_are_forty():
    assert ROUND_TRIP_FEES_USD == 40.0
    # Zero model move still deducts $40 plus the preserved FIX spread.
    flat = hedge_pnl_detail("SELL_USD", (FIX["bid"] + FIX["ask"]) / 2.0, FIX)
    assert flat is not None
    no_fee = flat["gross_pnl_usd"]
    assert abs((no_fee - 40.0) - flat["net_pnl_usd"]) < 1e-9
    assert flat["fees_usd"] == 40.0


def test_sell_usd_profitable_horizon():
    eval_spot = 17.80  # model sold and MXN strengthened
    out = historical_horizon_pnl("SELL_USD", MODEL_SPOT, eval_spot, FIX)
    assert out["pricing_basis"] == "fix"
    assert out["net_pnl_usd"] == _expected_net("SELL_USD", eval_spot)
    assert out["net_pnl_usd"] > 0
    # Re-anchored exit is FIX mid + move, not the model print.
    assert out["exit_rate"] < 17.0
    assert abs(out["exit_rate"] - (16.91 + (eval_spot - MODEL_SPOT) + 0.01)) < 1e-6


def test_sell_usd_directionally_correct_but_negative_after_costs():
    eval_spot = 17.995  # tiny correct move; spread + $40 dominate
    out = historical_horizon_pnl("SELL_USD", MODEL_SPOT, eval_spot, FIX)
    assert eval_spot < MODEL_SPOT  # directionally correct for SELL
    assert out["pricing_basis"] == "fix"
    assert out["net_pnl_usd"] < 0
    assert out["net_pnl_usd"] == _expected_net("SELL_USD", eval_spot)


def test_buy_usd_profitable_horizon():
    eval_spot = 18.20
    out = historical_horizon_pnl("BUY_USD", MODEL_SPOT, eval_spot, FIX)
    assert out["pricing_basis"] == "fix"
    assert out["net_pnl_usd"] == _expected_net("BUY_USD", eval_spot)
    assert out["net_pnl_usd"] > 0


def test_losing_horizon_is_negative():
    eval_spot = 18.15  # SELL_USD was wrong
    out = historical_horizon_pnl("SELL_USD", MODEL_SPOT, eval_spot, FIX)
    assert out["net_pnl_usd"] < 0
    assert out["pricing_basis"] == "fix"


def test_missing_historical_fix_returns_unavailable():
    out = historical_horizon_pnl("SELL_USD", MODEL_SPOT, 17.80, None)
    assert out == {"net_pnl_usd": None, "exit_rate": None, "pricing_basis": "unavailable"}
    assert stored_fix_quote(_reco(fix_bid=None, fix_ask=None)) is None


def test_does_not_mix_model_spot_with_fix_entry():
    """Regression: feed-basis gap must not appear as hedge P/L."""
    eval_spot = 18.00  # no model move
    out = historical_horizon_pnl("SELL_USD", MODEL_SPOT, eval_spot, FIX)
    # Wrong approach would close at model 18.00 after entering FIX 16.90.
    bogus = net_hedge_pnl_usd("SELL_USD", 18.00, FIX)
    assert bogus is not None and bogus < -5_000
    assert out["net_pnl_usd"] is not None
    assert out["net_pnl_usd"] > -200  # spread + fees only
    assert out["net_pnl_usd"] != bogus


def test_horizon_results_cover_required_cases():
    sell_win = _outcome(horizon="1h", spot_at_evaluation=17.80, direction_correct=True)
    sell_small = _outcome(horizon="4h", spot_at_evaluation=17.995, direction_correct=True)
    sell_loss = _outcome(horizon="end_of_day", spot_at_evaluation=18.15, direction_correct=False)
    buy_win = _outcome(horizon="1d", spot_at_evaluation=18.20, direction_correct=True)

    sell = serialize_history_row(
        _reco(outcomes=[sell_win, sell_small, sell_loss], direction="SELL_USD")
    )
    assert sell["horizon_status"]["1h"] == "Win"
    assert sell["horizon_status"]["4h"] == "Win"
    assert sell["horizon_status"]["end_of_day"] == "Loss"
    assert sell["horizon_status"]["2d"] == "Pending"
    assert sell["horizon_results"]["1h"]["status"] == "Win"
    assert sell["horizon_results"]["1h"]["net_pnl_usd"] > 0
    assert sell["horizon_results"]["1h"]["pricing_basis"] == "fix"
    assert sell["horizon_results"]["4h"]["status"] == "Win"
    assert sell["horizon_results"]["4h"]["net_pnl_usd"] < 0  # correct but not enough
    assert sell["horizon_results"]["end_of_day"]["status"] == "Loss"
    assert sell["horizon_results"]["end_of_day"]["net_pnl_usd"] < 0
    pending = sell["horizon_results"]["2d"]
    assert pending == {
        "status": "Pending",
        "net_pnl_usd": None,
        "exit_rate": None,
        "pricing_basis": None,
    }

    buy = serialize_history_row(_reco(direction="BUY_USD", outcomes=[buy_win]))
    assert buy["horizon_status"]["1d"] == "Win"
    assert buy["horizon_results"]["1d"]["net_pnl_usd"] > 0
    assert buy["horizon_results"]["1d"]["pricing_basis"] == "fix"

    missing = serialize_history_row(
        _reco(fix_bid=None, fix_ask=None, outcomes=[sell_win])
    )
    assert missing["horizon_status"]["1h"] == "Win"  # grading unchanged
    assert missing["horizon_results"]["1h"]["net_pnl_usd"] is None
    assert missing["horizon_results"]["1h"]["pricing_basis"] == "unavailable"


def test_horizon_status_still_returned_for_old_consumers():
    row = serialize_history_row(
        _reco(outcomes=[_outcome(horizon="1h", direction_correct=True)])
    )
    assert set(row["horizon_status"]) == {"1h", "4h", "end_of_day", "1d", "2d", "5d"}
    assert row["horizon_status"]["1h"] == "Win"
    assert row["horizon_status"]["5d"] == "Pending"
    assert "horizon_results" in row
    assert row["horizon_results"]["1h"]["status"] == row["horizon_status"]["1h"]


def test_history_api_keeps_horizon_status_contract():
    import os

    os.environ.setdefault("USE_MOCK_DATA", "true")
    from fastapi.testclient import TestClient

    from app.database import SessionLocal, init_db
    from app.main import app
    from app.models import Recommendation

    init_db()
    db = SessionLocal()
    try:
        reco = Recommendation(
            pair="USDMXN",
            spot_price=18.0,
            direction="SELL_USD",
            confidence=70.0,
            opportunity_grade="B",
            model_version="api-horizon-status",
            fix_bid=16.90,
            fix_ask=16.92,
        )
        db.add(reco)
        db.commit()
    finally:
        db.close()

    with TestClient(app) as client:
        body = client.get("/recommendations/history?limit=50").json()
    rows = {r["model_version"]: r for r in body["recommendations"]}
    row = rows["api-horizon-status"]
    assert "horizon_status" in row
    assert set(row["horizon_status"]) == {"1h", "4h", "end_of_day", "1d", "2d", "5d"}
    assert all(v == "Pending" for v in row["horizon_status"].values())
    assert "horizon_results" in row
