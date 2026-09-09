from types import SimpleNamespace

from app.services.horizon_calibration import calibrate_horizon
from app.services.recommendation_evaluator import HORIZONS


class _Result:
    def __init__(self, rows):
        self._rows = rows

    def all(self):
        return self._rows


class _DB:
    def __init__(self, rows):
        self.rows = rows

    def execute(self, _stmt):
        return _Result(self.rows)


def _row(ret, correct, confidence=72.0, horizon_confidence=72.0):
    outcome = SimpleNamespace(return_pct=ret, direction_correct=correct)
    reco = SimpleNamespace(
        confidence=confidence,
        time_horizons=[
            {"horizon": "1-4 hours", "confidence": horizon_confidence},
            {"horizon": "End of day", "confidence": horizon_confidence},
            {"horizon": "1-2 days", "confidence": horizon_confidence},
        ],
    )
    return outcome, reco


def test_evaluator_tracks_two_hour_horizon_separately():
    assert "1h" in HORIZONS
    assert "2h" in HORIZONS
    assert "4h" in HORIZONS
    assert HORIZONS.index("1h") < HORIZONS.index("2h") < HORIZONS.index("4h")


def test_reliable_horizon_calibration_shrinks_but_never_expands():
    rows = [_row(-0.10 - (i % 4) * 0.01, True) for i in range(24)]
    cal = calibrate_horizon(
        _DB(rows),
        horizon="4h",
        direction="SELL_USD",
        raw_move_pct=-0.50,
        raw_confidence=74.0,
    )
    assert cal["reliable"] is True
    assert cal["samples"] == 24
    assert cal["move_shrunk"] is True
    assert abs(cal["calibrated_move_pct"]) < 0.50

    small_raw = calibrate_horizon(
        _DB(rows),
        horizon="4h",
        direction="SELL_USD",
        raw_move_pct=-0.05,
        raw_confidence=74.0,
    )
    assert small_raw["calibrated_move_pct"] == -0.05
    assert small_raw["move_shrunk"] is False


def test_small_samples_do_not_change_forecast():
    rows = [_row(-0.12, True) for _ in range(7)]
    cal = calibrate_horizon(
        _DB(rows),
        horizon="1h",
        direction="SELL_USD",
        raw_move_pct=-0.40,
        raw_confidence=75.0,
    )
    assert cal["status"] == "provisional"
    assert cal["calibrated_move_pct"] == -0.40
    assert cal["calibrated_confidence"] == 75.0


def test_historical_horizon_confidence_controls_bucket_membership():
    rows = [
        _row(-0.10, True, confidence=80.0, horizon_confidence=72.0),
        _row(-0.12, True, confidence=80.0, horizon_confidence=45.0),
    ]
    cal = calibrate_horizon(
        _DB(rows),
        horizon="4h",
        direction="SELL_USD",
        raw_move_pct=-0.20,
        raw_confidence=74.0,
        min_samples=1,
    )
    assert cal["samples"] == 1
    assert cal["directional_accuracy"] == 100.0
