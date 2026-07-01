"""Unit tests for Phase A grade calibration."""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.services.grade_engine import (
    GRADE_BANDS,
    compute_opportunity_grade,
    grade_distribution,
    letter_from_score,
)
from app.services.market_data import MarketData


def _market(**kw) -> MarketData:
    defaults = dict(vix=15.0, usdmxn=20.0)
    defaults.update(kw)
    return MarketData(**defaults)


def _signal(**kw) -> dict:
    base = {
        "direction": "BUY_USD",
        "trade_score": 35.0,
        "confidence": 45.0,
        "risk_level": "low",
        "conflicting_signals": [
            {"strength": 0.4, "label": "Oil"},
            {"strength": 0.1, "label": "Gold"},
        ],
        "signal_breakdown": {
            "net_score": 8.0,
            "total_score": 25.0,
            "usd_score": 16.0,
            "mxn_score": 8.0,
        },
    }
    base.update(kw)
    return base


def test_pass_only_for_no_trade():
    legacy = compute_opportunity_grade(
        _signal(direction="NO_TRADE", confidence=10.0),
        {"primary": "Range Bound", "confidence": 30},
        _market(),
        version="legacy",
    )
    v2 = compute_opportunity_grade(
        _signal(direction="NO_TRADE", confidence=10.0),
        {"primary": "Range Bound", "confidence": 30},
        _market(),
        version="v2",
    )
    assert legacy["grade"] == "PASS"
    assert v2["grade"] == "PASS"


def test_directional_never_pass():
    g = compute_opportunity_grade(
        _signal(direction="BUY_USD", confidence=5.0, trade_score=5.0),
        {"primary": "Range Bound", "confidence": 10},
        _market(),
    )
    assert g["grade"] != "PASS"
    assert g["grade"] == "D"


def test_v2_reduces_conflict_penalty_vs_legacy():
    signal = _signal()
    regime = {"primary": "Fed Driven", "confidence": 40}
    market = _market()
    legacy = compute_opportunity_grade(signal, regime, market, version="legacy")
    v2 = compute_opportunity_grade(signal, regime, market, version="v2")
    assert legacy["components"]["conflict_penalty"] == 10.0  # 2 * 5
    assert v2["components"]["conflict_penalty"] == 3.0  # 1 material * 3


def test_blended_confidence_improves_grade():
    signal = _signal(confidence=40.0, trade_score=40.0)
    regime = {"primary": "Fed Driven", "confidence": 50}
    market = _market()
    pre = compute_opportunity_grade(signal, regime, market, version="v2")
    post = compute_opportunity_grade(
        signal, regime, market, version="v2", confidence_override=75.0
    )
    assert post["score"] >= pre["score"]


def test_grade_bands_unchanged():
    assert GRADE_BANDS[0] == (85.0, "A+")
    assert letter_from_score(84.9, "BUY_USD") == "A"
    assert letter_from_score(73.9, "BUY_USD") == "B"
    assert letter_from_score(45.9, "BUY_USD") == "D"


def test_strong_unanimous_can_reach_a():
    signal = _signal(
        trade_score=45.0,
        confidence=90.0,
        conflicting_signals=[],
        signal_breakdown={
            "net_score": 12.0,
            "total_score": 12.0,
            "usd_score": 12.0,
            "mxn_score": 0.0,
        },
    )
    g = compute_opportunity_grade(
        signal,
        {"primary": "Fed Driven", "confidence": 60},
        _market(),
        version="v2",
        confidence_override=85.0,
    )
    assert g["grade"] in {"A+", "A", "B"}


def test_grade_distribution_helper():
    dist = grade_distribution(["D", "D", "C", "A", "PASS"])
    assert dist["D"] == 2
    assert dist["PASS"] == 1


def main():
    tests = [
        test_pass_only_for_no_trade,
        test_directional_never_pass,
        test_v2_reduces_conflict_penalty_vs_legacy,
        test_blended_confidence_improves_grade,
        test_grade_bands_unchanged,
        test_strong_unanimous_can_reach_a,
        test_grade_distribution_helper,
    ]
    failed = 0
    for fn in tests:
        try:
            fn()
            print(f"PASS  {fn.__name__}")
        except Exception as exc:
            failed += 1
            print(f"FAIL  {fn.__name__}: {exc}")
    print(f"\n{len(tests) - failed} passed, {failed} failed")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
