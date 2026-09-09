from app.services.topline_forecast import _entry


def test_range_bound_forecast_does_not_show_directional_pnl():
    item = _entry(
        "1 hour",
        16.8923,
        "RANGE_BOUND",
        50.0,
        16.8919,
        grade="D",
        direction="SELL_USD",
        fix={"bid": 16.89, "ask": 16.90},
    )
    assert item["hedge_pnl_usd"] is None
    assert item["hedge_pnl_direction"] is None
    assert item["grade"] == "D"


def test_horizon_pnl_uses_forecast_direction_not_trade_lean():
    item = _entry(
        "End of day",
        16.95,
        "BUY_USD",
        55.0,
        16.89,
        grade="D",
        direction="SELL_USD",
        fix={"bid": 16.89, "ask": 16.90},
    )
    assert item["hedge_pnl_direction"] == "BUY_USD"
    assert item["hedge_pnl_usd"] is not None
    assert item["hedge_pnl_usd"] > 0
