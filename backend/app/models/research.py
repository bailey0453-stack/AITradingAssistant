"""Historical Research Database models (Phase 1)."""

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
    mx2y: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    mx10y: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    sp500: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    vix: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    gold: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    oil: Mapped[Optional[float]] = mapped_column(Float, nullable=True)

    # Intraday USD/MXN structure derived from hourly bars (% returns / volatility).
    momentum_1h: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    momentum_2h: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    momentum_4h: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    intraday_vol_4h: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    intraday_vol_24h: Mapped[Optional[float]] = mapped_column(Float, nullable=True)

    # Short-horizon forward USD/MXN returns from the day's final hourly bar.
    # These power the 1h/2h/4h topline forecast windows.
    ret_next_1h: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    ret_next_2h: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    ret_next_4h: Mapped[Optional[float]] = mapped_column(Float, nullable=True)

    # USD/MXN options market: implied vol and 25-delta skew/butterfly.
    iv_1w: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    iv_1m: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    rr25_1w: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    rr25_1m: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    butterfly_25d_1m: Mapped[Optional[float]] = mapped_column(Float, nullable=True)

    # Free CFTC Mexican-peso futures positioning (weekly, forward-filled daily).
    cftc_mxn_net: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    cftc_mxn_net_pct_oi: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    cftc_mxn_open_interest: Mapped[Optional[float]] = mapped_column(Float, nullable=True)

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
    """Future self-learning row: snapshot + recommendation + realized outcomes."""

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
