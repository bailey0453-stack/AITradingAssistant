"""Personal, user-confirmed entries and sampled FIX quotes; never broker orders."""
from datetime import datetime, timezone

from sqlalchemy import Boolean, DateTime, Float, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class PersonalTrade(Base):
    __tablename__ = "personal_fx_trades"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    owner_key: Mapped[str] = mapped_column(String(64), index=True)
    # Nullable unique slot permits multiple closed records but one open trade.
    owner_slot: Mapped[str | None] = mapped_column(String(64), unique=True, nullable=True)
    direction: Mapped[str] = mapped_column(String(16))
    entry_rate: Mapped[float] = mapped_column(Float)
    target_rate: Mapped[float] = mapped_column(Float)
    stop_rate: Mapped[float] = mapped_column(Float)
    opened_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    review_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    peak_rate: Mapped[float] = mapped_column(Float)
    last_quote_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    coverage_gap: Mapped[bool] = mapped_column(Boolean, default=False)
    exit_advice: Mapped[str | None] = mapped_column(String(32), nullable=True)
    advice_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    advice_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    exit_rate: Mapped[float | None] = mapped_column(Float, nullable=True)


class ObservedFixQuote(Base):
    __tablename__ = "observed_fix_quotes"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    minute: Mapped[datetime] = mapped_column(DateTime(timezone=True), unique=True)
    quote_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    bid: Mapped[float] = mapped_column(Float)
    ask: Mapped[float] = mapped_column(Float)
    seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))
