"""Focused tests for fixed-horizon vs first-net-profit exit comparison."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app.database import Base
from app.models import MarketSnapshot, Recommendation, RecommendationOutcome
from app.models.exit_simulations import (
    EXIT_FIRST_NET_PROFIT,
    EXIT_FIXED_HORIZON,
    RecommendationExitSimulation,
)
from app.services.exit_strategy_comparison import (
    compare_exit_strategies,
    cost_config,
    simulate_first_net_profit,
    simulate_fixed_horizon,
)


def _cfg():
    return cost_config()


def test_first_net_positive_observation_exits_correctly():
    cfg = _cfg()
    entry = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)
    # BUY_USD: rate rising after costs → first profitable mid
    ticks = [
        (entry + timedelta(minutes=10), 17.30, None, None),  # may still be net-negative after spread+costs
        (entry + timedelta(minutes=30), 17.50, None, None),  # clearly profitable
        (entry + timedelta(hours=2), 17.80, None, None),  # must NOT be chosen (look-ahead)
    ]
    sim = simulate_first_net_profit(
        direction="BUY_USD",
        entry_at=entry,
        spot_mid=17.30,
        ticks=ticks,
        cfg=cfg,
        terminal_mid=17.80,
        terminal_at=entry + timedelta(days=1),
    )
    assert sim["simulated_exit_reason"] == "FIRST_NET_PROFIT"
    assert sim["simulated_net_pnl"] > 0
    assert sim["simulated_exit_at"] == entry + timedelta(minutes=30)


def test_gross_profit_negative_after_costs_does_not_exit():
    cfg = _cfg()
    entry = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)
    # Tiny favorable move: gross may be small / net negative after $40 + spread
    ticks = [
        (entry + timedelta(minutes=15), 17.3005, None, None),
    ]
    sim = simulate_first_net_profit(
        direction="BUY_USD",
        entry_at=entry,
        spot_mid=17.30,
        ticks=ticks,
        cfg=cfg,
        terminal_mid=17.3005,
        terminal_at=entry + timedelta(days=1),
    )
    assert sim["simulated_exit_reason"] == "TERMINAL_FALLBACK"


def test_sell_usd_pnl_direction_correct():
    cfg = _cfg()
    entry = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)
    # SELL_USD profits when rate falls
    ticks = [
        (entry + timedelta(minutes=20), 17.10, None, None),
    ]
    sim = simulate_first_net_profit(
        direction="SELL_USD",
        entry_at=entry,
        spot_mid=17.30,
        ticks=ticks,
        cfg=cfg,
        terminal_mid=17.10,
        terminal_at=entry + timedelta(days=1),
    )
    assert sim["simulated_net_pnl"] > 0
    assert sim["simulated_exit_reason"] == "FIRST_NET_PROFIT"


def test_buy_usd_pnl_direction_correct():
    cfg = _cfg()
    entry = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)
    ticks = [
        (entry + timedelta(minutes=20), 17.50, None, None),
    ]
    sim = simulate_first_net_profit(
        direction="BUY_USD",
        entry_at=entry,
        spot_mid=17.30,
        ticks=ticks,
        cfg=cfg,
        terminal_mid=17.50,
        terminal_at=entry + timedelta(days=1),
    )
    assert sim["simulated_net_pnl"] > 0


def test_no_look_ahead_selection():
    cfg = _cfg()
    entry = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)
    ticks = [
        (entry + timedelta(minutes=10), 17.45, None, None),  # first profit
        (entry + timedelta(minutes=40), 17.90, None, None),  # better — must ignore
    ]
    sim = simulate_first_net_profit(
        direction="BUY_USD",
        entry_at=entry,
        spot_mid=17.30,
        ticks=ticks,
        cfg=cfg,
        terminal_mid=17.90,
        terminal_at=entry + timedelta(days=1),
    )
    assert sim["simulated_exit_at"] == entry + timedelta(minutes=10)
    # Exit rate must correspond to first tick, not the peak
    assert sim["simulated_exit_rate"] < 17.90


def test_no_profitable_observation_falls_back_to_terminal():
    cfg = _cfg()
    entry = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)
    ticks = [
        (entry + timedelta(minutes=30), 17.29, None, None),  # adverse for BUY
        (entry + timedelta(hours=2), 17.28, None, None),
    ]
    sim = simulate_first_net_profit(
        direction="BUY_USD",
        entry_at=entry,
        spot_mid=17.30,
        ticks=ticks,
        cfg=cfg,
        terminal_mid=17.25,
        terminal_at=entry + timedelta(hours=24),
    )
    assert sim["simulated_exit_reason"] == "TERMINAL_FALLBACK"
    assert sim["simulated_net_pnl"] is not None


def test_old_and_new_use_identical_costs_and_notional():
    cfg = _cfg()
    entry = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)
    old = simulate_fixed_horizon(
        direction="BUY_USD",
        entry_at=entry,
        spot_mid=17.30,
        terminal_mid=17.40,
        terminal_at=entry + timedelta(days=1),
        cfg=cfg,
    )
    new = simulate_first_net_profit(
        direction="BUY_USD",
        entry_at=entry,
        spot_mid=17.30,
        ticks=[(entry + timedelta(hours=1), 17.40, None, None)],
        cfg=cfg,
        terminal_mid=17.40,
        terminal_at=entry + timedelta(days=1),
    )
    assert old["simulated_costs"] == new["simulated_costs"] == cfg.total_cost_usd


def _session():
    # Ensure additive exit-sim table is registered; paper_positions is optional
    # local WIP and only needed when Recommendation declares that FK.
    from app.models import exit_simulations  # noqa: F401
    try:
        from app.models import paper_positions  # noqa: F401
    except ImportError:
        pass
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)()


def test_incomplete_history_excluded_and_disclosed():
    db = _session()
    created = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)
    reco = Recommendation(
        pair="USDMXN",
        direction="BUY_USD",
        spot_price=17.30,
        target=17.50,
        stop=17.10,
        confidence=80.0,
        opportunity_grade="A",
        created_at=created,
        model_version="test-v1",
    )
    db.add(reco)
    db.flush()
    # Only one post-entry tick + terminal at 1d → incomplete for first-profit
    db.add(
        MarketSnapshot(
            pair="USDMXN",
            usdmxn=17.31,
            created_at=created + timedelta(hours=1),
        )
    )
    db.add(
        RecommendationOutcome(
            recommendation_id=reco.id,
            horizon="1d",
            evaluated_at=created + timedelta(days=1),
            spot_at_evaluation=17.35,
            actionable=True,
            net_pnl_usd=10.0,
        )
    )
    db.commit()
    out = compare_exit_strategies(db, persist=True)
    assert out["excluded_count"] >= 1
    assert any(e["reason"] == "incomplete_intraday_observations" for e in out["excluded"])
    assert out["sample_completeness"] == "PARTIAL SAMPLE"


def test_fixed_horizon_records_not_overwritten():
    db = _session()
    created = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)
    reco = Recommendation(
        pair="USDMXN",
        direction="BUY_USD",
        spot_price=17.30,
        target=17.50,
        stop=17.10,
        confidence=80.0,
        opportunity_grade="A",
        created_at=created,
        model_version="test-v1",
    )
    db.add(reco)
    db.flush()
    original_net = 123.45
    outcome = RecommendationOutcome(
        recommendation_id=reco.id,
        horizon="1d",
        evaluated_at=created + timedelta(days=1),
        spot_at_evaluation=17.50,
        actionable=True,
        net_pnl_usd=original_net,
        gross_pnl_usd=163.45,
    )
    db.add(outcome)
    # Enough ticks for complete first-profit path
    for i, px in enumerate([17.35, 17.42, 17.48, 17.50]):
        db.add(
            MarketSnapshot(
                pair="USDMXN",
                usdmxn=px,
                created_at=created + timedelta(minutes=30 * (i + 1)),
            )
        )
    db.commit()
    compare_exit_strategies(db, persist=True)
    db.refresh(outcome)
    assert outcome.net_pnl_usd == original_net
    sims = db.execute(
        select(RecommendationExitSimulation).where(
            RecommendationExitSimulation.recommendation_id == reco.id
        )
    ).scalars().all()
    policies = {s.exit_policy for s in sims}
    assert EXIT_FIXED_HORIZON in policies
    assert EXIT_FIRST_NET_PROFIT in policies
