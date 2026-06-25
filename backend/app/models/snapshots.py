"""Persisted snapshots for market data and AI analysis.

JSON-ish fields (news, economic calendar, key drivers) are stored as JSON via
SQLAlchemy's generic JSON type, which maps to TEXT on SQLite and JSON/JSONB on
Postgres.
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

    # Placeholder macro drivers (populated by providers; mocked for now)
    dxy: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    treasury_yield: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    oil: Mapped[Optional[float]] = mapped_column(Float, nullable=True)

    # Unstructured / list placeholders
    news: Mapped[Optional[list]] = mapped_column(JSON, nullable=True)
    economic_calendar: Mapped[Optional[list]] = mapped_column(JSON, nullable=True)

    source: Mapped[str] = mapped_column(String(32), default="mock")

    analyses: Mapped[List["AnalysisSnapshot"]] = relationship(
        back_populates="market_snapshot",
        cascade="all, delete-orphan",
    )


class AnalysisSnapshot(Base):
    __tablename__ = "analysis_snapshots"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, index=True
    )

    pair: Mapped[str] = mapped_column(String(16), default="USDMXN", index=True)

    # Signal
    direction: Mapped[str] = mapped_column(String(16))  # BUY_USD | SELL_USD | NO_TRADE
    confidence: Mapped[float] = mapped_column(Float)  # 0..100

    summary: Mapped[str] = mapped_column(Text, default="")
    key_drivers: Mapped[Optional[list]] = mapped_column(JSON, nullable=True)

    target: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    stretch_target: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    stop: Mapped[Optional[float]] = mapped_column(Float, nullable=True)

    momentum_status: Mapped[str] = mapped_column(String(64), default="")
    risk_notes: Mapped[str] = mapped_column(Text, default="")

    model: Mapped[str] = mapped_column(String(64), default="mock-rules-v1")

    market_snapshot_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("market_snapshots.id"), nullable=True, index=True
    )
    market_snapshot: Mapped[Optional["MarketSnapshot"]] = relationship(
        back_populates="analyses"
    )
