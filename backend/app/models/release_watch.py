"""Live release observations, kept separate from sample/backfilled research."""
from datetime import datetime, timezone

from sqlalchemy import DateTime, Float, Integer, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class ReleaseWatch(Base):
    __tablename__ = "live_release_watch"
    __table_args__ = (UniqueConstraint("event_key", name="uq_live_release_event"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    event_key: Mapped[str] = mapped_column(String(64), index=True)
    event_name: Mapped[str] = mapped_column(String(180))
    series: Mapped[str] = mapped_column(String(64), index=True)
    country: Mapped[str] = mapped_column(String(8))
    unit: Mapped[str] = mapped_column(String(24))
    reference: Mapped[str] = mapped_column(String(48), default="")
    release_time: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    source: Mapped[str] = mapped_column(String(48))
    consensus: Mapped[float | None] = mapped_column(Float, nullable=True)
    consensus_seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    previous: Mapped[float | None] = mapped_column(Float, nullable=True)
    revised_previous: Mapped[float | None] = mapped_column(Float, nullable=True)
    actual: Mapped[float | None] = mapped_column(Float, nullable=True)
    latest_actual: Mapped[float | None] = mapped_column(Float, nullable=True)
    actual_seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    pre_bid: Mapped[float | None] = mapped_column(Float, nullable=True)
    pre_ask: Mapped[float | None] = mapped_column(Float, nullable=True)
    pre_quote_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    outcome_20m: Mapped[float | None] = mapped_column(Float, nullable=True)
    outcome_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))
