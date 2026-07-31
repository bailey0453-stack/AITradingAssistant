"""Focused tests for the top-of-dashboard trade decision card."""

from __future__ import annotations

from app.services.trade_decision_card import build_trade_decision_card


def _base(**overrides):
    payload = {
        "direction": "SELL_USD",
        "opportunity_grade": "A",
        "confidence": 80.0,
        "stop": 17.40,
        "market": {"usdmxn": 17.3375, "source": "live"},
        "market_state": {"is_open": True, "is_stale": False, "cached": False, "age_minutes": 1},
        "topline_forecast": {
            "now": 17.3375,
            "horizons": [
                {"horizon": "4 hours", "expected_rate": 17.2942, "bias": "SELL_USD"},
                {"horizon": "End of day", "expected_rate": 17.2681, "bias": "SELL_USD"},
            ],
            "long_usd_bailout": 17.2681,
            "short_usd_bailout": 17.4068,
        },
        "decision_quality": {
            "should_trade_now": True,  # must be ignored for TRADE gate
            "expected_value": {"expected_value_usd": 120.0},
            "similar_track_record": {
                "enough_history": True,
                "similar_avg_pnl": 85.0,
                "similar_win_rate": 62.0,
            },
            "components": {"event_risk": 100.0},
            "high_impact_event_count": 0,
        },
        "context": {"upcoming_events": []},
        "provenance": {},
    }
    payload.update(overrides)
    return payload


def test_grade_b_negative_ev_returns_wait():
    card = build_trade_decision_card(
        _base(
            opportunity_grade="B",
            decision_quality={
                "should_trade_now": True,
                "expected_value": {"expected_value_usd": -40.0},
                "similar_track_record": {
                    "enough_history": True,
                    "similar_avg_pnl": 10.0,
                    "similar_win_rate": 55.0,
                },
                "components": {"event_risk": 100.0},
                "high_impact_event_count": 0,
            },
        )
    )
    assert card["action"] == "WAIT"
    assert card["visual"] == "yellow"
    assert card["prediction"] == "USD/MXN LOWER"


def test_grade_a_positive_ev_no_event_returns_trade():
    card = build_trade_decision_card(_base())
    assert card["action"] == "TRADE"
    assert card["visual"] == "green"
    assert card["bias"] == "SELL USD / BUY MXN"
    assert card["prediction"] == "USD/MXN LOWER"


def test_high_impact_event_changes_trade_to_wait():
    base = _base()
    assert build_trade_decision_card(base)["action"] == "TRADE"
    card = build_trade_decision_card(
        _base(
            decision_quality={
                **base["decision_quality"],
                "high_impact_event_count": 1,
                "components": {"event_risk": 75.0},
            },
            context={"upcoming_events": [{"importance": "high", "title": "CPI"}]},
        )
    )
    assert card["action"] == "WAIT"
    assert "event" in card["why"].lower() or any("event" in r.lower() for r in card["wait_reasons"])


def test_stale_required_data_changes_trade_to_wait():
    base = _base()
    assert build_trade_decision_card(base)["action"] == "TRADE"
    card = build_trade_decision_card(
        _base(market_state={"is_open": True, "is_stale": True, "cached": True, "age_minutes": 999})
    )
    assert card["action"] == "WAIT"


def test_invalidation_level_returns_exit():
    card = build_trade_decision_card(
        _base(
            market={"usdmxn": 17.45, "source": "live"},
            topline_forecast={
                "now": 17.45,
                "horizons": [
                    {"horizon": "4 hours", "expected_rate": 17.2942, "bias": "SELL_USD"},
                    {"horizon": "End of day", "expected_rate": 17.2681, "bias": "SELL_USD"},
                ],
                "long_usd_bailout": 17.2681,
                "short_usd_bailout": 17.4068,
            },
        )
    )
    assert card["action"] == "EXIT / INVALIDATED"
    assert card["visual"] == "red"


def test_sell_usd_shows_lower_prediction():
    card = build_trade_decision_card(_base(direction="SELL_USD"))
    assert card["prediction"] == "USD/MXN LOWER"
    assert card["bias"] == "SELL USD / BUY MXN"


def test_buy_usd_shows_higher_prediction():
    card = build_trade_decision_card(
        _base(
            direction="BUY_USD",
            stop=17.20,
            topline_forecast={
                "now": 17.3375,
                "horizons": [
                    {"horizon": "4 hours", "expected_rate": 17.38, "bias": "BUY_USD"},
                    {"horizon": "End of day", "expected_rate": 17.40, "bias": "BUY_USD"},
                ],
                "long_usd_bailout": 17.2681,
                "short_usd_bailout": 17.4068,
            },
        )
    )
    assert card["prediction"] == "USD/MXN HIGHER"
    assert card["bias"] == "BUY USD / SELL MXN"


def test_wait_still_displays_directional_forecast():
    card = build_trade_decision_card(
        _base(
            opportunity_grade="B",
            decision_quality={
                "should_trade_now": False,
                "expected_value": {"expected_value_usd": -10.0},
                "similar_track_record": {"enough_history": False, "similar_avg_pnl": None},
                "components": {"event_risk": 100.0},
                "high_impact_event_count": 0,
            },
        )
    )
    assert card["action"] == "WAIT"
    assert card["has_directional_forecast"] is True
    assert card["prediction"] == "USD/MXN LOWER"
    assert card["predicted_4h"] == 17.2942
