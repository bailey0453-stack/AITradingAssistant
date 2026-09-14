"""Focused tests for the tactical multi-window momentum layer."""

from app.services.intraday_momentum import aggregate


def test_aligned_rising_windows_create_positive_tactical_score():
    result = aggregate({
        "change": 0.004,
        "from": 17.10,
        "to": 17.125,
        "windows": {
            "5m": {"change": 0.010, "from": 17.115, "to": 17.125, "minutes": 5},
            "15m": {"change": 0.018, "from": 17.107, "to": 17.125, "minutes": 15},
            "30m": {"change": 0.025, "from": 17.100, "to": 17.125, "minutes": 30},
            "60m": {"change": 0.035, "from": 17.090, "to": 17.125, "minutes": 60},
        },
        "acceleration_per_minute": 0.0002,
    })
    assert result is not None
    assert result["tactical_score"] > 0.5
    assert result["change"] > 0
    assert len(result["window_diagnostics"]) == 4


def test_short_term_reversal_can_overcome_slower_trend():
    result = aggregate({
        "change": -0.008,
        "from": 17.13,
        "to": 17.122,
        "windows": {
            "5m": {"change": -0.012, "from": 17.134, "to": 17.122, "minutes": 5},
            "15m": {"change": -0.018, "from": 17.140, "to": 17.122, "minutes": 15},
            "30m": {"change": 0.010, "from": 17.112, "to": 17.122, "minutes": 30},
            "60m": {"change": 0.020, "from": 17.102, "to": 17.122, "minutes": 60},
        },
        "acceleration_per_minute": -0.0004,
    })
    assert result is not None
    assert result["tactical_score"] < 0
    assert result["change"] < 0


def test_legacy_consecutive_momentum_remains_supported():
    result = aggregate({"change": 0.01, "from": 17.10, "to": 17.11})
    assert result == {"change": 0.01, "from": 17.10, "to": 17.11}
