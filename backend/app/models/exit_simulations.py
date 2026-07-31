"""Additive simulated exit evaluations (does not overwrite fixed-horizon outcomes)."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import DateTime, Float, ForeignKey, Integer, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base

EXIT_FIXED_HORIZON = "fixed_horizon"
EXIT_FIRST_NET_PROFIT = "first_net_profit"


class RecommendationExitSimulation(Base):
    """One simulated exit path per recommendation + exit policy.

    Additive and reproducible. Never mutates ``recommendation_outcomes``.
    """

    __tablename__ = "recommendation_exit_simulations"
    __table_args__ = (
        UniqueConstraint(
            "recommendation_id",
            "exit_policy",
            "exit_policy_version",
            name="uq_reco_exit_policy_version",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    recommendation_id: Mapped[int] = mapped_column(
        ForeignKey("recommendations.id"), nullable=False, index=True
    )
    exit_policy: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    exit_policy_version: Mapped[str] = mapped_column(String(32), nullable=False, default="v1")

    simulated_exit_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    simulated_exit_rate: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    simulated_exit_reason: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    simulated_gross_pnl: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    simulated_costs: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    simulated_net_pnl: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    holding_minutes: Mapped[Optional[float]] = mapped_column(Float, nullable=True)

    data_completeness: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        nullable=False,
    )
