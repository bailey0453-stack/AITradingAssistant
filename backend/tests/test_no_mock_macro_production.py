from datetime import datetime, timezone

from app.config import Settings
from app.services import macro_data, market_data
from app.services.signal_weights import score_signals


def _settings() -> Settings:
    return Settings(
        use_mock_data=False,
        fred_api_key="x" * 32,
        alpha_vantage_api_key="y" * 16,
    )


def _live_fix(monkeypatch, rate: float = 17.0) -> None:
    monkeypatch.setattr(
        market_data,
        "_fresh_fix_midpoint",
        lambda *args, **kwargs: (rate, datetime.now(timezone.utc).isoformat()),
    )


def test_unavailable_macro_is_never_replaced_with_mock_values(monkeypatch):
    """Failed live macro inputs must contribute zero, never random mock evidence."""
    _live_fix(monkeypatch)
    monkeypatch.setattr(macro_data, "fetch_live_macro", lambda settings: {})

    data = market_data.get_market_data(_settings())

    assert data.usdmxn == 17.0
    assert data.field_sources["usdmxn"] == "live"
    for field in market_data.MACRO_FIELDS:
        assert getattr(data, field) is None, (field, getattr(data, field))
        assert data.field_sources[field] == "unavailable", (field, data.field_sources)

    # Missing production macro fields generate zero deltas and therefore no
    # directional contributions when no other evidence is supplied.
    assert data.drivers["dxy_delta"] == 0.0
    assert data.drivers["yield_delta"] == 0.0
    assert data.drivers["us2y_delta"] == 0.0
    assert data.drivers["oil_delta"] == 0.0
    assert data.drivers["gold_delta"] == 0.0
    assert data.drivers["sp_delta"] == 0.0
    assert data.drivers["vix_delta"] == 0.0

    scored = score_signals(data, news=[], released_events=[], momentum=None)
    assert scored["weighted_contributions"] == []
    assert scored["net_score"] == 0.0
    assert scored["direction"] == "HOLD"


def test_partial_live_macro_keeps_only_verified_fields(monkeypatch):
    """A partial provider response may score its live fields but nothing else."""
    _live_fix(monkeypatch, 16.95)
    monkeypatch.setattr(
        macro_data,
        "fetch_live_macro",
        lambda settings: {
            "us2y": 4.71,
            "us10y": 4.29,
            "treasury_yield": 4.29,
            "oil": 77.2,
        },
    )

    data = market_data.get_market_data(_settings())

    assert data.us2y == 4.71
    assert data.us10y == 4.29
    assert data.treasury_yield == 4.29
    assert data.oil == 77.2
    assert data.field_sources["us2y"] == "live"
    assert data.field_sources["us10y"] == "live"
    assert data.field_sources["oil"] == "live"

    for field in ("dxy", "gold", "sp_futures", "vix"):
        assert getattr(data, field) is None, (field, getattr(data, field))
        assert data.field_sources[field] == "unavailable", (field, data.field_sources)
