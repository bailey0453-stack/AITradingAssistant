"""Paper AI recommendation tracking (model signals) + outcome evaluation.

These are **paper recommendations / model signals**, deliberately kept separate
from any real executed trade. A future real-trade table can link back via
``recommendation_id``; we never mix the two datasets.

Tables
------
- ``recommendations``         — one lean, indexed row per ``/analysis/usdmxn``
  signal (denormalized from ``analysis_snapshots`` for fast performance queries).
- ``recommendation_outcomes`` — one row per (recommendation, horizon) once enough
  time has passed to score it.

Designed for many rows: timestamp / direction / confidence / opportunity_grade /
evaluated_at are indexed, and outcomes are unique per (recommendation, horizon).
"""

from datetime import datetime, timezone
from typing import List, Optional

from sqlalchemy import (
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    JSON,
    String,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Recommendation(Base):
    """A stored paper recommendation (model signal) for later evaluation."""

    __tablename__ = "recommendations"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    # Recommendation timestamp (indexed for time-range queries / growth).
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, index=True
    )

    pair: Mapped[str] = mapped_column(String(16), default="USDMXN", index=True)
    spot_price: Mapped[Optional[float]] = mapped_column(Float, nullable=True)

    direction: Mapped[str] = mapped_column(String(16), index=True)
    confidence: Mapped[Optional[float]] = mapped_column(Float, index=True)
    opportunity_grade: Mapped[Optional[str]] = mapped_column(
        String(8), nullable=True, index=True
    )
    trade_score: Mapped[Optional[float]] = mapped_column(Float, nullable=True)

    market_regime: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)

    target: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    stretch_target: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    stop: Mapped[Optional[float]] = mapped_column(Float, nullable=True)

    time_horizons: Mapped[Optional[list]] = mapped_column(JSON, nullable=True)
    key_drivers: Mapped[Optional[list]] = mapped_column(JSON, nullable=True)
    historical_similarity: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)
    strategist: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)

    # Evaluation bookkeeping (kept light; details live in outcomes).
    evaluation_status: Mapped[str] = mapped_column(
        String(16), default="pending", index=True
    )  # pending | partial | complete
    last_evaluated_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True, index=True
    )

    # Provenance links (nullable) — never merged into one table.
    analysis_snapshot_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("analysis_snapshots.id"), nullable=True, index=True
    )
    market_snapshot_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("market_snapshots.id"), nullable=True, index=True
    )

    outcomes: Mapped[List["RecommendationOutcome"]] = relationship(
        back_populates="recommendation", cascade="all, delete-orphan"
    )


class RecommendationOutcome(Base):
    """Scored result of a recommendation at a specific horizon."""

    __tablename__ = "recommendation_outcomes"
    __table_args__ = (
        UniqueConstraint("recommendation_id", "horizon", name="uq_reco_horizon"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow
    )

    recommendation_id: Mapped[int] = mapped_column(
        ForeignKey("recommendations.id"), index=True
    )
    recommendation: Mapped["Recommendation"] = relationship(back_populates="outcomes")

    evaluated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, index=True
    )
    horizon: Mapped[str] = mapped_column(String(16), index=True)  # 1h|4h|end_of_day|1d|2d|5d

    spot_at_evaluation: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    return_pct: Mapped[Optional[float]] = mapped_column(Float, nullable=True)

    direction_correct: Mapped[Optional[bool]] = mapped_column(Boolean, nullable=True)
    target_hit: Mapped[Optional[bool]] = mapped_column(Boolean, nullable=True)
    stretch_hit: Mapped[Optional[bool]] = mapped_column(Boolean, nullable=True)
    stop_hit: Mapped[Optional[bool]] = mapped_column(Boolean, nullable=True)

    max_favorable_excursion: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    max_adverse_excursion: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
