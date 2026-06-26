"""Persisted snapshots for market data, news, and AI analysis.

JSON-ish fields (news, calendar, key drivers, context, timeline) are stored as
JSON via SQLAlchemy's generic JSON type, which maps to TEXT on SQLite and
JSON/JSONB on Postgres.
"""

from datetime import datetime, timezone
from typing import List, Optional

from sqlalchemy import JSON, DateTime, Float, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class MarketSnapshot(Base):
    __tablename__ = "market_snapshots"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, index=True
    )

    pair: Mapped[str] = mapped_column(String(16), default="USDMXN", index=True)

    # Core tracked instrument
    usdmxn: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    inverse_usdmxn: Mapped[Optional[float]] = mapped_column(Float, nullable=True)

    # Macro drivers (mocked placeholders until live macro providers exist)
    dxy: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    us2y: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    us10y: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    treasury_yield: Mapped[Optional[float]] = mapped_column(Float, nullable=True)  # legacy alias of us10y
    oil: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    gold: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    sp_futures: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    vix: Mapped[Optional[float]] = mapped_column(Float, nullable=True)

    # Unstructured / list placeholders
    news: Mapped[Optional[list]] = mapped_column(JSON, nullable=True)
    economic_calendar: Mapped[Optional[list]] = mapped_column(JSON, nullable=True)

    provider: Mapped[str] = mapped_column(String(48), default="mock")
    source: Mapped[str] = mapped_column(String(32), default="mock")

    analyses: Mapped[List["AnalysisSnapshot"]] = relationship(
        back_populates="market_snapshot",
        cascade="all, delete-orphan",
    )


class NewsItem(Base):
    __tablename__ = "news_items"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, index=True
    )

    headline: Mapped[str] = mapped_column(Text)
    summary: Mapped[str] = mapped_column(Text, default="")
    source: Mapped[str] = mapped_column(String(96), default="")
    url: Mapped[str] = mapped_column(Text, default="")
    published_at: Mapped[Optional[str]] = mapped_column(String(40), nullable=True, index=True)

    sentiment: Mapped[str] = mapped_column(String(24), default="neutral")
    affected_currencies: Mapped[Optional[list]] = mapped_column(JSON, nullable=True)
    importance: Mapped[str] = mapped_column(String(16), default="low")
    tags: Mapped[Optional[list]] = mapped_column(JSON, nullable=True)

    provider: Mapped[str] = mapped_column(String(48), default="mock")


class AnalysisSnapshot(Base):
    __tablename__ = "analysis_snapshots"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, index=True
    )

    pair: Mapped[str] = mapped_column(String(16), default="USDMXN", index=True)

    # Signal
    direction: Mapped[str] = mapped_column(String(16))  # BUY_USD | SELL_USD | NO_TRADE
    trade_score: Mapped[Optional[float]] = mapped_column(Float, nullable=True)  # 0..100
    market_bias: Mapped[str] = mapped_column(String(32), default="")
    confidence: Mapped[float] = mapped_column(Float)  # 0..100
    momentum_status: Mapped[str] = mapped_column(String(64), default="")
    risk_level: Mapped[str] = mapped_column(String(16), default="")

    summary: Mapped[str] = mapped_column(Text, default="")
    key_drivers: Mapped[Optional[list]] = mapped_column(JSON, nullable=True)

    # Explanatory breakdown (Phase 3)
    market_drivers: Mapped[Optional[list]] = mapped_column(JSON, nullable=True)
    bullish_factors: Mapped[Optional[list]] = mapped_column(JSON, nullable=True)
    bearish_factors: Mapped[Optional[list]] = mapped_column(JSON, nullable=True)
    upcoming_risks: Mapped[Optional[list]] = mapped_column(JSON, nullable=True)

    # Configurable weighting engine output (for debugging / tuning)
    weighted_contributions: Mapped[Optional[list]] = mapped_column(JSON, nullable=True)
    conflicting_signals: Mapped[Optional[list]] = mapped_column(JSON, nullable=True)
    signal_breakdown: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)

    # Reasoning engine output (Phase 3.5)
    market_regime: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)
    opportunity_grade: Mapped[Optional[str]] = mapped_column(String(8), nullable=True)
    opportunity_grade_detail: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)
    what_would_change_my_mind: Mapped[Optional[list]] = mapped_column(JSON, nullable=True)

    # Trade levels
    entry: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    target: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    stretch_target: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    stop: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    invalidation_level: Mapped[Optional[float]] = mapped_column(Float, nullable=True)

    expected_move: Mapped[str] = mapped_column(String(64), default="")
    expected_duration: Mapped[str] = mapped_column(String(64), default="")
    historical_similarity: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)

    risk_notes: Mapped[str] = mapped_column(Text, default="")

    # Stored context (becomes the backtesting / similarity dataset)
    news_context: Mapped[Optional[list]] = mapped_column(JSON, nullable=True)
    calendar_context: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)
    timeline: Mapped[Optional[list]] = mapped_column(JSON, nullable=True)

    model: Mapped[str] = mapped_column(String(64), default="mock-rules-v1")

    market_snapshot_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("market_snapshots.id"), nullable=True, index=True
    )
    market_snapshot: Mapped[Optional["MarketSnapshot"]] = relationship(
        back_populates="analyses"
    )
