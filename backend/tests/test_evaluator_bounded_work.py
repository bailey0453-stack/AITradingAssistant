"""Evaluation stays bounded when prices stop, without losing catch-up outcomes."""

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import create_engine, event, select
from sqlalchemy.orm import Session

from app.database import Base
from app.models import MarketSnapshot, Recommendation, RecommendationOutcome
from app.services import recommendation_evaluator as evaluator


UTC = timezone.utc
FRIDAY = datetime(2026, 10, 16, tzinfo=UTC)


@pytest.fixture
def evaluation_engine():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    yield engine
    engine.dispose()


def _recommendation(db, created, *, pair="USDMXN", scored=()):
    reco = Recommendation(
        created_at=created, pair=pair, direction="BUY_USD", spot_price=18.0,
        target=18.1, stretch_target=18.2, stop=17.9,
        evaluation_status="partial" if scored else "pending",
    )
    db.add(reco)
    db.flush()
    for horizon in scored:
        db.add(RecommendationOutcome(recommendation_id=reco.id, horizon=horizon))
    return reco.id


def _price(db, timestamp, *, pair="USDMXN", value=18.05):
    db.add(MarketSnapshot(
        created_at=timestamp, pair=pair, usdmxn=value, source="live",
    ))


def _selects(engine):
    statements = []

    def capture(conn, cursor, statement, parameters, context, executemany):
        if statement.startswith("SELECT"):
            statements.append(statement)

    event.listen(engine, "before_cursor_execute", capture)
    return statements


def test_closed_weekend_does_not_scan_each_unpriceable_horizon(evaluation_engine):
    with Session(evaluation_engine) as db:
        _price(db, FRIDAY + timedelta(hours=21))
        for _ in range(1000):
            _recommendation(
                db, FRIDAY + timedelta(hours=16),
                scored=("1h", "2h", "4h", "end_of_day"),
            )
        db.commit()

    statements = _selects(evaluation_engine)
    with Session(evaluation_engine) as db:
        result = evaluator.evaluate_due(
            db, FRIDAY + timedelta(days=1, hours=19), limit=1,
        )

    assert result == {"evaluated": 0, "recommendations_touched": 0, "completed": 0}
    # One price cutoff query and one eligibility query, independent of backlog.
    assert len(statements) == 2


def test_ineligible_oldest_rows_do_not_starve_available_newer_row(evaluation_engine):
    short_horizons = ("1h", "2h", "4h", "end_of_day")
    with Session(evaluation_engine) as db:
        for _ in range(20):
            _recommendation(db, FRIDAY + timedelta(hours=16), scored=short_horizons)
        ready_id = _recommendation(db, FRIDAY + timedelta(hours=19))
        _price(db, FRIDAY + timedelta(hours=20), value=18.04)
        _price(db, FRIDAY + timedelta(hours=21), value=18.08)
        db.commit()

    with Session(evaluation_engine) as db:
        result = evaluator.evaluate_due(
            db, FRIDAY + timedelta(days=1, hours=19), limit=1,
        )
        outcome = db.execute(
            select(RecommendationOutcome)
            .where(RecommendationOutcome.recommendation_id == ready_id)
        ).scalar_one()
        assert result == {"evaluated": 1, "recommendations_touched": 1, "completed": 0}
        assert outcome.horizon == "1h"
        # The latest quote is the eligibility cutoff, not the evaluation price.
        assert outcome.spot_at_evaluation == 18.04


def test_available_backlog_respects_limit_and_bulk_reads_outcomes(evaluation_engine):
    scored = tuple(h for h in evaluator.HORIZONS if h != "1h")
    with Session(evaluation_engine) as db:
        ids = [
            _recommendation(db, FRIDAY - timedelta(days=8) + timedelta(minutes=i), scored=scored)
            for i in range(30)
        ]
        _price(db, FRIDAY + timedelta(hours=20))
        db.commit()

    statements = _selects(evaluation_engine)
    with Session(evaluation_engine) as db:
        result = evaluator.evaluate_due(db, FRIDAY + timedelta(hours=21), limit=2)
    # Three batch reads, then the original price and path queries per outcome.
    assert len(statements) == 7
    assert result == {"evaluated": 2, "recommendations_touched": 2, "completed": 2}
    with Session(evaluation_engine) as db:
        completed_ids = db.execute(
            select(Recommendation.id).where(Recommendation.evaluation_status == "complete")
            .order_by(Recommendation.id)
        ).scalars().all()
    assert completed_ids == ids[:2]
    assert all("recommendations.strategist" not in sql for sql in statements)


@pytest.mark.parametrize("latest_offset,expected_eod", [
    (timedelta(hours=20, minutes=59, seconds=59), False),
    (timedelta(hours=21), True),
    (timedelta(hours=21, microseconds=1), True),
])
def test_end_of_day_uses_last_reached_close(evaluation_engine, latest_offset, expected_eod):
    scored = tuple(h for h in evaluator.HORIZONS if h != "end_of_day")
    with Session(evaluation_engine) as db:
        _recommendation(db, FRIDAY + timedelta(hours=20, minutes=59, seconds=59), scored=scored)
        _price(db, FRIDAY + latest_offset)
        db.commit()
    with Session(evaluation_engine) as db:
        result = evaluator.evaluate_due(db, FRIDAY + timedelta(days=1), limit=1)
    assert result["evaluated"] == int(expected_eod)


def test_recommendation_exactly_at_close_waits_until_next_close(evaluation_engine):
    scored = tuple(h for h in evaluator.HORIZONS if h != "end_of_day")
    with Session(evaluation_engine) as db:
        _recommendation(db, FRIDAY + timedelta(hours=21), scored=scored)
        _price(db, FRIDAY + timedelta(hours=21))
        _price(db, FRIDAY + timedelta(days=1, hours=20, minutes=59))
        db.commit()
    with Session(evaluation_engine) as db:
        assert evaluator.evaluate_due(db, FRIDAY + timedelta(days=1, hours=21))["evaluated"] == 0
        _price(db, FRIDAY + timedelta(days=1, hours=21))
        db.commit()
        assert evaluator.evaluate_due(db, FRIDAY + timedelta(days=1, hours=21))["evaluated"] == 1


def test_price_cutoff_is_per_pair_and_excludes_null_and_future_prices(evaluation_engine):
    scored = tuple(h for h in evaluator.HORIZONS if h != "1h")
    with Session(evaluation_engine) as db:
        for pair in ("OLD", "NULL", "FUTURE", "READY"):
            _recommendation(db, FRIDAY + timedelta(hours=16), pair=pair, scored=scored)
        _price(db, FRIDAY + timedelta(hours=16), pair="OLD")
        _price(db, FRIDAY + timedelta(hours=17), pair="NULL", value=None)
        _price(db, FRIDAY + timedelta(days=1), pair="FUTURE")
        _price(db, FRIDAY + timedelta(hours=17), pair="READY")
        db.commit()
    with Session(evaluation_engine) as db:
        result = evaluator.evaluate_due(db, FRIDAY + timedelta(hours=18), limit=1)
        completed_pairs = db.execute(
            select(Recommendation.pair).where(Recommendation.evaluation_status == "complete")
        ).scalars().all()
    assert result["evaluated"] == 1
    assert completed_pairs == ["READY"]


def test_closed_market_still_scores_available_catch_up_outcomes(evaluation_engine):
    with Session(evaluation_engine) as db:
        reco_id = _recommendation(db, FRIDAY + timedelta(hours=16))
        for hour, value in ((17, 18.04), (18, 18.06), (20, 18.12), (21, 18.10)):
            _price(db, FRIDAY + timedelta(hours=hour), value=value)
        db.commit()
    with Session(evaluation_engine) as db:
        now = FRIDAY + timedelta(days=1, hours=19)
        result = evaluator.evaluate_due(db, now)
        assert result == {"evaluated": 4, "recommendations_touched": 1, "completed": 0}
        outcomes = db.execute(
            select(RecommendationOutcome)
            .where(RecommendationOutcome.recommendation_id == reco_id)
        ).scalars().all()
        assert {r.horizon for r in outcomes} == {"1h", "2h", "4h", "end_of_day"}
        four_hour = next(r for r in outcomes if r.horizon == "4h")
        assert four_hour.target_hit is True
        assert four_hour.spot_at_evaluation == 18.12
        assert evaluator.evaluate_due(db, now)["evaluated"] == 0


def test_repeated_small_batches_finish_each_horizon_once(evaluation_engine):
    created = FRIDAY - timedelta(days=8)
    with Session(evaluation_engine) as db:
        reco_id = _recommendation(db, created)
        for horizon in evaluator.HORIZONS:
            _price(db, evaluator.horizon_due_time(created, horizon))
        db.commit()
    with Session(evaluation_engine) as db:
        batches = [evaluator.evaluate_due(db, FRIDAY, limit=2) for _ in range(5)]
        assert [batch["evaluated"] for batch in batches] == [2, 2, 2, 1, 0]
        assert sum(batch["completed"] for batch in batches) == 1
        horizons = db.execute(
            select(RecommendationOutcome.horizon)
            .where(RecommendationOutcome.recommendation_id == reco_id)
        ).scalars().all()
        assert sorted(horizons) == sorted(evaluator.HORIZONS)


@pytest.mark.parametrize("limit", [0, -1])
def test_nonpositive_limit_performs_no_database_work(evaluation_engine, limit):
    statements = _selects(evaluation_engine)
    with Session(evaluation_engine) as db:
        assert evaluator.evaluate_due(db, FRIDAY, limit=limit) == {
            "evaluated": 0, "recommendations_touched": 0, "completed": 0,
        }
    assert statements == []


def test_no_available_prices_returns_without_loading_recommendations(evaluation_engine):
    with Session(evaluation_engine) as db:
        _recommendation(db, FRIDAY - timedelta(days=8))
        db.commit()
    statements = _selects(evaluation_engine)
    with Session(evaluation_engine) as db:
        assert evaluator.evaluate_due(db, FRIDAY)["evaluated"] == 0
    assert len(statements) == 1
