"""Historical intelligence models (Phase 4).

These tables are the **public / backfillable** historical dataset plus a derived
similarity cache. They are kept deliberately separate from:

  - the proprietary *live* recommendation history (`AnalysisSnapshot` in
    `snapshots.py`), and
  - any future Border Currency trade-outcome data (not modeled here).

Tables
------
- ``historical_market_snapshots`` — backfilled market time series (USD/MXN +
  macro), one row per observation. Public/free data.
- ``historical_events``           — backfilled economic events (type, time,
  forecast/actual/surprise). Public/free data.
- ``historical_event_reactions``  — how USD/MXN reacted after each event over
  fixed windows, with the pre-event market context. Derived from the two tables
  above.
- ``similarity_matches``          — a cache of "events like now" comparisons. It
  references the public ``historical_events`` and *optionally* a proprietary
  ``analysis_snapshots`` row, but never merges the two datasets into one table.

All numeric reaction values are **percent returns** of USD/MXN unless noted.
JSON columns map to TEXT on SQLite and JSON/JSONB on Postgres.
"""

from datetime import datetime, timezone
from typing import List, Optional

from sqlalchemy import JSON, DateTime, Float, ForeignKey, Integer, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class HistoricalMarketSnapshot(Base):
    """Backfilled market time-series point (USD/MXN + macro context)."""

    __tablename__ = "historical_market_snapshots"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)

    series: Mapped[str] = mapped_column(String(16), default="USDMXN", index=True)
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)

    usdmxn: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    dxy: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    us2y: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    us10y: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    oil: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    gold: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    vix: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    sp_futures: Mapped[Optional[float]] = mapped_column(Float, nullable=True)

    regime: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    # Provenance — never confuse a vendor tick with a reconstructed proxy.
    source: Mapped[str] = mapped_column(String(48), default="sample")
    source_quality: Mapped[str] = mapped_column(String(16), default="sample")

    # Optional event linkage for points that belong to a reaction path.
    event_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("historical_events.id"), nullable=True, index=True
    )


class HistoricalEvent(Base):
    """Backfilled economic event (release) with forecast/actual/surprise."""

    __tablename__ = "historical_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)

    event_type: Mapped[str] = mapped_column(String(48), index=True)  # us_cpi, us_nfp, ...
    event_name: Mapped[str] = mapped_column(String(128), default="")
    country: Mapped[str] = mapped_column(String(8), default="US")
    release_time: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)

    forecast: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    actual: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    previous: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    surprise: Mapped[Optional[float]] = mapped_column(Float, nullable=True)      # actual - forecast
    surprise_z: Mapped[Optional[float]] = mapped_column(Float, nullable=True)    # normalized surprise

    importance: Mapped[str] = mapped_column(String(16), default="medium")
    currency_impact: Mapped[str] = mapped_column(String(8), default="USD")

    source: Mapped[str] = mapped_column(String(48), default="sample")
    source_quality: Mapped[str] = mapped_column(String(16), default="sample")

    reactions: Mapped[List["HistoricalEventReaction"]] = relationship(
        back_populates="event", cascade="all, delete-orphan"
    )


class HistoricalEventReaction(Base):
    """USD/MXN reaction to an event over fixed windows + pre-event context.

    Returns are percent moves of USD/MXN from the price at the event time;
    positive = USD strengthened (USD/MXN up), negative = peso strengthened.
    """

    __tablename__ = "historical_event_reactions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)

    event_id: Mapped[int] = mapped_column(
        ForeignKey("historical_events.id"), index=True
    )
    event: Mapped["HistoricalEvent"] = relationship(back_populates="reactions")

    # Denormalized event descriptors for fast similarity scoring.
    event_type: Mapped[str] = mapped_column(String(48), index=True)
    surprise: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    surprise_z: Mapped[Optional[float]] = mapped_column(Float, nullable=True)

    series: Mapped[str] = mapped_column(String(16), default="USDMXN")
    baseline_price: Mapped[Optional[float]] = mapped_column(Float, nullable=True)

    # Reaction windows (percent return of USD/MXN).
    ret_15m: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    ret_1h: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    ret_4h: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    ret_1d: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    ret_3d: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    ret_5d: Mapped[Optional[float]] = mapped_column(Float, nullable=True)

    # Excursions over the full (5d) window, percent.
    max_favorable_excursion: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    max_adverse_excursion: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    time_to_peak_hours: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    reversal_behavior: Mapped[str] = mapped_column(String(24), default="unknown")
    data_completeness: Mapped[float] = mapped_column(Float, default=1.0)  # 0..1

    # Pre-event market context (the feature vector for similarity).
    dxy: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    us2y: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    us10y: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    oil: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    gold: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    vix: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    sp_futures: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    momentum: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    regime: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    news_tags: Mapped[Optional[list]] = mapped_column(JSON, nullable=True)

    source: Mapped[str] = mapped_column(String(48), default="sample")
    source_quality: Mapped[str] = mapped_column(String(16), default="sample")


class SimilarityMatch(Base):
    """Cache of a single "events like now" comparison (derived, proprietary).

    Links the live query to a public ``historical_events`` row. May reference a
    proprietary ``analysis_snapshots`` row, but the two datasets stay in their
    own tables.
    """

    __tablename__ = "similarity_matches"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)

    query_context: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)
    matched_event_id: Mapped[int] = mapped_column(
        ForeignKey("historical_events.id"), index=True
    )
    reaction_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("historical_event_reactions.id"), nullable=True
    )
    similarity_score: Mapped[float] = mapped_column(Float, default=0.0)
    rank: Mapped[int] = mapped_column(Integer, default=0)

    # Optional, nullable link to the proprietary recommendation that triggered it.
    analysis_snapshot_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("analysis_snapshots.id"), nullable=True, index=True
    )
