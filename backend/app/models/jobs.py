"""Scheduled-job run log.

One row per scheduled job invocation (e.g. the hourly USD/MXN analysis cron) so
the dashboard can show *when the system last ran on its own*, independent of any
user opening the page. Deliberately lean and append-only.
"""

from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import Boolean, DateTime, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class JobRun(Base):
    """A single scheduled-job execution and its outcome."""

    __tablename__ = "job_runs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    ran_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, index=True
    )
    job_name: Mapped[str] = mapped_column(String(48), default="hourly-usdmxn-analysis", index=True)

    created_recommendation: Mapped[bool] = mapped_column(Boolean, default=False)
    recommendation_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    market_status: Mapped[Optional[str]] = mapped_column(String(16), nullable=True)
    market_source: Mapped[Optional[str]] = mapped_column(String(16), nullable=True)
    evaluated_outcomes_count: Mapped[int] = mapped_column(Integer, default=0)
    skipped_reason: Mapped[Optional[str]] = mapped_column(String(48), nullable=True)
