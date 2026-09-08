from app.services import topline_forecast as tf


def _payload(direction="SELL_USD", moves=None):
    moves = moves or {"1h": -0.08, "4h": -0.22, "1d": -0.31}
    matches = []
    for i in range(7):
        matches.append({
            "similarity_score": 0.9 - i * 0.03,
            "windows": {k: v * (1 + (i - 3) * 0.04) for k, v in moves.items()},
            "max_adverse_excursion": 0.14 + i * 0.01,
        })
    return {
        "direction": direction,
        "opportunity_grade": "B",
        "market": {"usdmxn": 17.0},
        "historical": {"historical_source": "backfilled", "top_matches": matches},
        "time_horizons": [
            {"horizon": "1-4 hours", "bias": direction, "confidence": 62},
            {"horizon": "End of day", "bias": direction, "confidence": 58},
            {"horizon": "1-2 days", "bias": direction, "confidence": 55},
        ],
    }


def test_targets_are_measured_from_analogs_not_fixed_percentages():
    payload = _payload()
    one, one_ev = tf._analog_move(payload, "1h", "SELL_USD")
    four, four_ev = tf._analog_move(payload, "4h", "SELL_USD")
    day, day_ev = tf._analog_move(payload, "1d", "SELL_USD")
    assert one_ev["status"] == four_ev["status"] == day_ev["status"] == "measured"
    assert round(one, 2) == -0.08
    assert round(four, 2) == -0.22
    assert round(day, 2) == -0.31
    # Changing the analog evidence changes the forecast distance; there is no
    # horizon constant such as the former -0.25% / -0.40% / -0.60% path.
    stronger = _payload(moves={"1h": -0.13, "4h": -0.47, "1d": -0.71})
    four2, _ = tf._analog_move(stronger, "4h", "SELL_USD")
    assert round(four2, 2) == -0.47
    assert four2 != four


def test_numeric_target_is_withheld_when_evidence_disagrees():
    payload = _payload(moves={"1h": 0.1, "4h": 0.2, "1d": 0.3})
    move, evidence = tf._analog_move(payload, "4h", "SELL_USD")
    assert move is None
    assert evidence["status"] == "evidence_disagrees"


def test_numeric_target_is_withheld_for_small_sample():
    payload = _payload()
    payload["historical"]["top_matches"] = payload["historical"]["top_matches"][:4]
    move, evidence = tf._analog_move(payload, "4h", "SELL_USD")
    assert move is None
    assert evidence["status"] == "insufficient_sample"


def test_two_hour_target_is_derived_from_measured_endpoints():
    move, evidence = tf._two_hour_move(-0.09, -0.30)
    assert round(move, 2) == -0.16
    assert evidence["status"] == "derived"
    missing, evidence2 = tf._two_hour_move(None, -0.30)
    assert missing is None and evidence2["status"] == "unavailable"


def test_bailout_uses_historical_adverse_excursion():
    payload = _payload()
    long_level, short_level, evidence = tf._evidence_bailouts(payload, 17.0, "SELL_USD")
    assert evidence["status"] == "measured"
    assert evidence["adverse_excursion_pct"] == 0.17
    assert long_level == round(17.0 * (1 - 0.0017), 4)
    assert short_level == round(17.0 * (1 + 0.0017), 4)
