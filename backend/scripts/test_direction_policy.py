#!/usr/bin/env python3
"""Unit tests for direction vs stand-aside policy."""

from __future__ import annotations

import sys

from app.services.direction_policy import apply_stand_aside, build_direction_reasoning, conviction_tier
from app.services.signal_weights import score_signals
from app.services.market_data import MarketData


def _flat_market() -> MarketData:
    return MarketData(
        usdmxn=18.5,
        dxy=104.0,
        us2y=4.5,
        us10y=4.3,
        oil=75.0,
        gold=2300.0,
        sp_futures=5200.0,
        vix=15.0,
        source="mock",
    )


def _bullish_market() -> MarketData:
    m = _flat_market()
    m.drivers = {
        "dxy_delta": 0.6,
        "yield_delta": 0.08,
        "us2y_delta": 0.08,
        "oil_delta": -2.5,
        "gold_delta": -25.0,
        "sp_delta": -40.0,
        "vix_delta": 2.0,
    }
    return m


def test_flat_market_is_hold_not_no_trade():
    scored = score_signals(_flat_market())
    assert scored["direction"] == "HOLD", scored["direction"]
    assert not scored["is_actionable"]
    signal, reason = apply_stand_aside(scored)
    assert signal["direction"] == "HOLD"
    assert reason is None


def test_strong_signal_is_actionable_buy():
    scored = score_signals(_bullish_market())
    assert scored["direction"] == "BUY_USD"
    assert scored["is_actionable"]


def test_critical_event_forces_stand_aside_on_hold():
    scored = score_signals(_flat_market())
    assert scored["direction"] == "HOLD"
    signal, reason = apply_stand_aside(
        scored,
        upcoming_events=[{"event": "US CPI", "importance": "high", "hours_away": 6.0}],
    )
    assert signal["direction"] == "NO_TRADE", signal["direction"]
    assert reason and "CPI" in reason


def test_critical_event_keeps_directional_bias():
    scored = score_signals(_bullish_market())
    signal, reason = apply_stand_aside(
        scored,
        upcoming_events=[{"event": "FOMC", "importance": "high", "hours_away": 12.0}],
    )
    assert signal["direction"] == "BUY_USD"
    assert reason is None


def test_direction_reasoning_includes_support_and_oppose():
    scored = score_signals(_bullish_market())
    assert scored["direction"] == "BUY_USD"
    assert scored["net_score"] >= 4.0
    dr = build_direction_reasoning(scored, bullish_factors=["DXY firm"], bearish_factors=[])
    assert dr["directional_bias"] == "BUY_USD"
    assert dr["supporting_signals"]
    assert dr["is_actionable"] is True
    assert dr["conviction_tier"] == "high"


def test_conviction_tiers():
    assert conviction_tier(0.1) == "none"
    assert conviction_tier(0.5) == "low"
    assert conviction_tier(2.5) == "medium"
    assert conviction_tier(5.0) == "high"


def main() -> int:
    tests = [
        test_flat_market_is_hold_not_no_trade,
        test_strong_signal_is_actionable_buy,
        test_critical_event_forces_stand_aside_on_hold,
        test_critical_event_keeps_directional_bias,
        test_direction_reasoning_includes_support_and_oppose,
        test_conviction_tiers,
    ]
    failed = 0
    for t in tests:
        try:
            t()
            print(f"  PASS  {t.__name__}")
        except AssertionError as e:
            failed += 1
            print(f"  FAIL  {t.__name__}: {e}")
    print(f"\n{len(tests) - failed}/{len(tests)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
