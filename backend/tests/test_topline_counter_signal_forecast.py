from app.services.topline_forecast import _analog_move


def test_analog_move_remains_numeric_when_history_disagrees_with_trade_lean():
    payload = {
        "historical": {
            "historical_source": "backfilled",
            "top_matches": [
                {"similarity_score": 0.9, "windows": {"4h": 0.10}},
                {"similarity_score": 0.8, "windows": {"4h": 0.12}},
                {"similarity_score": 0.7, "windows": {"4h": 0.08}},
                {"similarity_score": 0.6, "windows": {"4h": 0.11}},
                {"similarity_score": 0.5, "windows": {"4h": 0.09}},
            ],
        }
    }

    move, evidence = _analog_move(payload, "4h", "SELL_USD")

    assert move is not None
    assert move > 0
    assert evidence["status"] == "measured"
    assert evidence["trade_lean_agreement"] is False


def test_analog_move_still_withholds_when_sample_is_too_small():
    payload = {
        "historical": {
            "top_matches": [
                {"similarity_score": 0.9, "windows": {"1h": -0.02}},
                {"similarity_score": 0.8, "windows": {"1h": -0.01}},
            ]
        }
    }

    move, evidence = _analog_move(payload, "1h", "SELL_USD")

    assert move is None
    assert evidence["status"] == "insufficient_sample"
