"""Historical Research Database models (Phase 1).

Normalized daily market snapshots power the Historical Evidence panel and
similarity search. ``ResearchDailyLearning`` is the future self-learning layer
for storing each day's recommendation and realized outcomes alongside the
snapshot — populated automatically once the hourly pipeline is extended.
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from typing import List, Optional

from sqlalchemy import JSON, Date, DateTime, Float, ForeignKey, Integer, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class ResearchMarketSnapshot(Base):
    """One normalized market environment per trading day (10+ years target)."""

    __tablename__ = "research_market_snapshots"
    __table_args__ = (UniqueConstraint("trade_date", name="uq_research_trade_date"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)

    trade_date: Mapped[date] = mapped_column(Date, index=True)

    # Core FX + cross-asset panel
    usdmxn: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    dxy: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    us2y: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    us10y: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    sp500: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    vix: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    gold: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    oil: Mapped[Optional[float]] = mapped_column(Float, nullable=True)

    # Policy / inflation
    fed_funds: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    banxico_rate: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    us_cpi: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    mexico_cpi: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    us_pce: Mapped[Optional[float]] = mapped_column(Float, nullable=True)

    # Classification + context
    regime: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    economic_events: Mapped[Optional[list]] = mapped_column(JSON, nullable=True)
    volatility_20d: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    momentum_5d: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    momentum_20d: Mapped[Optional[float]] = mapped_column(Float, nullable=True)

    # Forward USD/MXN returns (%), measured from this day's close.
    ret_next_1d: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    ret_next_5d: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    ret_next_30d: Mapped[Optional[float]] = mapped_column(Float, nullable=True)

    source: Mapped[str] = mapped_column(String(48), default="research")
    source_quality: Mapped[str] = mapped_column(String(16), default="imported")


class ResearchDailyLearning(Base):
    """Future self-learning row: snapshot + recommendation + realized outcomes.

    Not populated in Phase 1; schema supports the proprietary research loop.
    """

    __tablename__ = "research_daily_learning"
    __table_args__ = (UniqueConstraint("trade_date", name="uq_learning_trade_date"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, onupdate=_utcnow)

    trade_date: Mapped[date] = mapped_column(Date, index=True)
    market_snapshot_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("research_market_snapshots.id"), nullable=True, index=True
    )

    recommendation_uuid: Mapped[Optional[str]] = mapped_column(String(64), nullable=True, index=True)
    direction: Mapped[Optional[str]] = mapped_column(String(16), nullable=True)
    confidence: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    opportunity_grade: Mapped[Optional[str]] = mapped_column(String(8), nullable=True)
    supporting_signals: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)

    model_version: Mapped[Optional[str]] = mapped_column(String(16), nullable=True)
    reasoning_engine_version: Mapped[Optional[str]] = mapped_column(String(16), nullable=True)
    weighting_profile: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    historical_engine_version: Mapped[Optional[str]] = mapped_column(String(16), nullable=True)

    ret_1h: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    ret_4h: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    ret_eod: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    ret_1d: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    ret_5d: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    self_evaluation: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)
