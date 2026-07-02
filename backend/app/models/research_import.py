"""Historical research import job tracking (admin / cron)."""

from __future__ import annotations

from datetime import date, datetime, timezone
from typing import Optional

from sqlalchemy import JSON, Date, DateTime, Float, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class HistoricalImportJob(Base):
    """One full or incremental research-database import run."""

    __tablename__ = "historical_import_jobs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    job_uuid: Mapped[str] = mapped_column(String(36), unique=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, onupdate=_utcnow)
    completed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)

    mode: Mapped[str] = mapped_column(String(16), default="full")  # full | incremental
    status: Mapped[str] = mapped_column(String(16), default="pending", index=True)
    importer: Mapped[str] = mapped_column(String(32), default="research")
    current_stage: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    stages_completed: Mapped[Optional[list]] = mapped_column(JSON, default=list)
    stages_skipped: Mapped[Optional[list]] = mapped_column(JSON, default=list)
    progress_pct: Mapped[float] = mapped_column(Float, default=0.0)

    lookback_days: Mapped[int] = mapped_column(Integer, default=3650)
    since_date: Mapped[Optional[date]] = mapped_column(Date, nullable=True)

    series_points: Mapped[int] = mapped_column(Integer, default=0)
    events_imported: Mapped[int] = mapped_column(Integer, default=0)
    reactions_imported: Mapped[int] = mapped_column(Integer, default=0)
    snapshots_built: Mapped[int] = mapped_column(Integer, default=0)

    errors: Mapped[Optional[list]] = mapped_column(JSON, default=list)
    stage_log: Mapped[Optional[list]] = mapped_column(JSON, default=list)
    summary: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)
    message: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
