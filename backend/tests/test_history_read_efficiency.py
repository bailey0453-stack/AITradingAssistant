"""Real-database regressions for bounded, narrow recommendation-history reads."""

from datetime import datetime, timedelta, timezone
from collections import defaultdict

import pytest
from sqlalchemy import create_engine, event, select
from sqlalchemy.orm import Session

from app.database import Base
from app.models import MarketSnapshot, Recommendation, RecommendationOutcome
from app.services import current_calibration, decision_quality, horizon_calibration
from app.services import research_lab as rl
from app.services import recommendation_evaluator as evaluator


@pytest.fixture
def history_engine():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        for i in range(40):
            created = datetime(2026, 8, 1, tzinfo=timezone.utc) + timedelta(days=i)
            reco = Recommendation(
                created_at=created, direction="BUY_USD" if i % 2 else "SELL_USD",
                confidence=90.0 if i % 2 else 75.0, opportunity_grade="A" if i % 2 else "B",
                trade_score=80.0 - i, regime="trend", volatility=18.0,
                news_category="policy", historical_similarity={"score": 0.9},
                model_version="v1" if i < 20 else "v2", spot_price=18.0, target=18.1,
                key_drivers=["rates", {"driver": "policy"}], bullish_factors=["growth"],
                bearish_factors=["risk"],
                time_horizons=[{"horizon": "1-4 hours", "confidence": 90.0}],
                strategist={"large_report": "x" * 20000},
                primary_trade_plan={"large_report": "x" * 20000},
            )
            db.add(reco)
            db.flush()
            for j, horizon in enumerate(("1d", "4h", "1h")):
                db.add(RecommendationOutcome(
                    recommendation_id=reco.id, horizon=horizon,
                    evaluated_at=created + timedelta(days=1, seconds=j),
                    direction_correct=None if i % 7 == 0 else i % 3 != 0,
                    target_hit=i % 3 != 0, stop_hit=i % 3 == 0,
                    spot_at_evaluation=18.05, return_pct=0.25 if i % 3 else -0.1,
                    actionable=True, gross_pnl_usd=100.0 if i % 3 else -50.0,
                    net_pnl_usd=60.0 if i % 3 else -90.0,
                ))
        db.commit()
    yield engine
    engine.dispose()


def _statements(engine):
    statements = []
    event.listen(engine, "before_cursor_execute", lambda conn, cursor, sql, *args: statements.append(sql))
    return statements


def _joined_reads(statements):
    return [s for s in statements if "JOIN recommendations" in s]


@pytest.mark.parametrize("limit", [0, 1, 2, 3, 17, 50000])
def test_accuracy_preserves_global_window_and_null_scores(history_engine, limit):
    with Session(history_engine) as db:
        scores = db.execute(
            select(RecommendationOutcome.horizon, RecommendationOutcome.direction_correct)
            .join(Recommendation)
            .order_by(RecommendationOutcome.evaluated_at.desc()).limit(limit)
        ).all()
    expected = rl._rate([score for horizon, score in scores if horizon == "1d"])
    statements = _statements(history_engine)
    with Session(history_engine) as db:
        assert rl.overall_accuracy(db, limit) == expected
    assert len(statements) == 1
    assert "count(" in statements[0].lower()
    assert "recommendations.strategist" not in statements[0]


@pytest.mark.parametrize("function", [rl.research_summary, rl.monthly_performance, rl.self_assessment])
def test_report_loads_history_once_without_large_payloads(history_engine, function):
    statements = _statements(history_engine)
    with Session(history_engine) as db:
        report = function(db)
    assert report
    joined = _joined_reads(statements)
    assert len(joined) == 1
    assert "recommendations.strategist" not in joined[0]
    assert "recommendations.primary_trade_plan" not in joined[0]
    # No deferred single-row SELECTs may silently restore the large payloads.
    assert not any(s.startswith("SELECT") and "WHERE recommendations.id =" in s for s in statements)


def test_summary_remains_fresh_after_new_outcome(history_engine):
    with Session(history_engine) as db:
        before = rl.research_summary(db)
        reco = Recommendation(direction="BUY_USD", confidence=90.0)
        db.add(reco)
        db.flush()
        db.add(RecommendationOutcome(recommendation_id=reco.id, horizon="1d", direction_correct=True))
        db.commit()
        after = rl.research_summary(db)
        assert after["overall"]["samples"] == before["overall"]["samples"] + 1
        assert after["overall_accuracy"] == rl.overall_accuracy(db)


@pytest.mark.parametrize("limit", [0, 1, 2, 17, 10000])
def test_performance_aggregates_preserve_report_and_global_window(history_engine, limit):
    with Session(history_engine) as db:
        # Independent reference using the original row-based calculations.
        pairs = db.execute(
            select(RecommendationOutcome, Recommendation).join(Recommendation)
            .order_by(RecommendationOutcome.evaluated_at.desc()).limit(limit)
        ).all()
        flat = []
        by_conf, by_grade, by_horizon = (defaultdict(list) for _ in range(3))
        for outcome, reco in pairs:
            row = {name: getattr(outcome, name) for name in (
                "return_pct", "direction_correct", "target_hit", "stop_hit",
            )}
            flat.append(row)
            by_conf[evaluator._bucket(reco.confidence)].append(row)
            by_grade[reco.opportunity_grade or "n/a"].append(row)
            by_horizon[outcome.horizon].append(row)
        overall = evaluator._agg(flat)
        expected = {
            "total_recommendations": 40, "evaluated_outcomes": len(flat),
            "overall": overall,
            **{name: overall[name] for name in (
                "win_rate", "target_hit_rate", "stop_hit_rate", "avg_return_pct",
            )},
            "by_confidence": {k: evaluator._agg(v) for k, v in sorted(by_conf.items())},
            "by_grade": {k: evaluator._agg(v) for k, v in sorted(by_grade.items())},
            "by_horizon": {h: evaluator._agg(by_horizon[h]) for h in evaluator.HORIZONS},
        }
    statements = _statements(history_engine)
    with Session(history_engine) as db:
        assert evaluator.performance_summary(db, limit) == expected
    assert len(statements) == 2
    assert "count(" in statements[0].lower()
    assert "UNION ALL" in statements[1]
    assert not any("recommendations.strategist" in sql or
                   "recommendations.primary_trade_plan" in sql for sql in statements)


def test_performance_empty_buckets_nulls_and_fresh_writes():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        empty = evaluator.performance_summary(db)
        assert empty["total_recommendations"] == empty["evaluated_outcomes"] == 0
        assert empty["overall"] == evaluator._agg([])
        assert empty["by_confidence"] == empty["by_grade"] == {}
        for i, confidence in enumerate((None, -1, 0, 50, 70, 85, 100, 100.01)):
            reco = Recommendation(direction="WAIT", confidence=confidence,
                                  opportunity_grade="" if i % 2 else None)
            db.add(reco)
            db.flush()
            db.add(RecommendationOutcome(
                recommendation_id=reco.id, horizon="1h", return_pct=None,
                direction_correct=None, target_hit=None, stop_hit=None,
            ))
        db.commit()
        report = evaluator.performance_summary(db)
        assert report["total_recommendations"] == report["evaluated_outcomes"] == 8
        assert {k: v["samples"] for k, v in report["by_confidence"].items()} == {
            "unknown": 3, "0-50": 1, "50-70": 1, "70-85": 1, "85-100": 2,
        }
        assert report["by_grade"]["n/a"]["samples"] == 8
        assert report["win_rate"] is None
        assert report["avg_return_pct"] is None
        assert report["by_horizon"]["5d"] == evaluator._agg([])
    engine.dispose()


def test_empty_summary_does_not_reload_empty_pairs():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    statements = _statements(engine)
    with Session(engine) as db:
        assert rl.research_summary(db)["overall_accuracy"] is None
        assert rl.overall_accuracy(db) is None
    assert len(_joined_reads(statements)) == 2  # one report read, one aggregate
    engine.dispose()


def test_live_calibrators_use_narrow_reads(history_engine, monkeypatch):
    monkeypatch.setattr(current_calibration, "SessionLocal", lambda: Session(history_engine))
    statements = _statements(history_engine)
    assert current_calibration.calibration_for_confidence(90.0)["samples"] == 20
    with Session(history_engine) as db:
        cal = horizon_calibration.calibrate_horizon(
            db, horizon="4h", direction="BUY_USD", raw_move_pct=0.5,
            raw_confidence=90.0, min_samples=1,
        )
        assert cal["samples"] == 17  # null direction scores are excluded
        assert len(decision_quality._primary_pairs(db)) == 40
    assert len(_joined_reads(statements)) == 3
    assert all("recommendations.strategist" not in s for s in statements)
    assert all("recommendations.primary_trade_plan" not in s for s in statements)


def test_evaluator_scores_all_horizons_once_with_narrow_pending_read():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    created = datetime(2026, 8, 3, 10, tzinfo=timezone.utc)
    with Session(engine) as db:
        reco = Recommendation(
            created_at=created, direction="BUY_USD", confidence=90.0,
            spot_price=18.0, target=18.1, stretch_target=18.3, stop=17.5,
            strategist={"large_report": "x" * 20000},
        )
        db.add(reco)
        for horizon in evaluator.HORIZONS:
            db.add(MarketSnapshot(
                pair="USDMXN", created_at=evaluator.horizon_due_time(created, horizon), usdmxn=18.2,
            ))
        db.commit()
    statements = _statements(engine)
    with Session(engine) as db:
        now = created + timedelta(days=6)
        assert evaluator.evaluate_due(db, now)["evaluated"] == len(evaluator.HORIZONS)
        assert evaluator.evaluate_due(db, now)["evaluated"] == 0
        outcomes = db.execute(select(RecommendationOutcome)).scalars().all()
        assert {o.horizon for o in outcomes} == set(evaluator.HORIZONS)
        assert all(o.target_hit and o.net_pnl_usd > 0 for o in outcomes)
    assert "recommendations.strategist" not in statements[0]
    assert not any(s.startswith("SELECT") and "WHERE recommendations.id =" in s for s in statements)
    engine.dispose()
