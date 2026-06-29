"""Scheduled (cron) jobs.

The hourly USD/MXN job lets the system generate and store paper recommendations
on its own — even when nobody opens the dashboard — and score any prior
recommendations that have come due.

Quota / safety rules baked in here:

- **Market-hours aware.** When the FX market is closed (weekend / holiday) we
  never request a live quote (no API quota burned) and we do not generate a new
  recommendation. We *do* still evaluate due recommendations (read-only).
- **No duplicates.** At most one job-generated recommendation per clock hour for
  a given pair + model version.
- **No fabricated data.** When live data is unavailable and there is no fresh
  cached real quote, we skip generation (stale-fallback safety is enforced by
  the analysis pipeline itself).
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import Settings, get_settings
from app.models import JobRun, Recommendation
from app.services import cache_manager
from app.services.market_hours import MarketStatus, get_market_state
from app.services.recommendation_evaluator import evaluate_due
from app.versions import MODEL_VERSION

logger = logging.getLogger(__name__)

JOB_NAME = "hourly-usdmxn-analysis"
PAIR = "USDMXN"
SCHEDULE_CRON = "0 * * * *"  # top of every hour (UTC), see vercel.json


def _aware(dt: datetime) -> datetime:
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _hour_bucket(dt: datetime) -> datetime:
    return _aware(dt).replace(minute=0, second=0, microsecond=0)


def _latest_reco(db: Session, pair: str) -> Optional[Recommendation]:
    return db.execute(
        select(Recommendation)
        .where(Recommendation.pair == pair)
        .order_by(Recommendation.id.desc())
        .limit(1)
    ).scalars().first()


def _existing_this_hour(
    db: Session, pair: str, model_version: str, now: datetime
) -> Optional[int]:
    """Return the id of any recommendation already stored this clock hour.

    Robust against SQLite's tz handling by comparing hour buckets in Python.
    """
    bucket = _hour_bucket(now)
    rows = db.execute(
        select(Recommendation)
        .where(Recommendation.pair == pair)
        .where(Recommendation.model_version == model_version)
        .order_by(Recommendation.created_at.desc())
        .limit(24)
    ).scalars().all()
    for reco in rows:
        if reco.created_at and _hour_bucket(reco.created_at) == bucket:
            return reco.id
    return None


def _latest_snapshot_source(db: Session) -> Optional[str]:
    # Lazy import avoids a router<->service import cycle at module load.
    from app.routers.market import _latest_snapshot

    latest = _latest_snapshot(db)
    return latest.source if latest else None


def _record_run(db: Session, summary: dict) -> None:
    try:
        db.add(JobRun(
            job_name=summary.get("job", JOB_NAME),
            created_recommendation=bool(summary.get("created_recommendation")),
            recommendation_id=summary.get("recommendation_id"),
            market_status=summary.get("market_status"),
            market_source=summary.get("market_source"),
            evaluated_outcomes_count=int(summary.get("evaluated_outcomes_count") or 0),
            skipped_reason=summary.get("skipped_reason"),
        ))
        db.commit()
    except Exception:  # noqa: BLE001 - logging the run must never fail the job
        logger.exception("Failed to persist JobRun")
        db.rollback()


def run_hourly_usdmxn_job(
    db: Session, settings: Optional[Settings] = None, *, now: Optional[datetime] = None
) -> dict:
    """Run the hourly USD/MXN analysis job and return a job summary."""
    settings = settings or get_settings()
    now = _aware(now or datetime.now(timezone.utc))

    summary: dict = {
        "job": JOB_NAME,
        "ran_at": now.isoformat(),
        "created_recommendation": False,
        "recommendation_id": None,
        "market_status": None,
        "market_source": None,
        "evaluated_outcomes_count": 0,
        "skipped_reason": None,
    }

    # Market state WITHOUT any provider fetch — pure, so it never burns quota.
    from app.routers.market import _market_calendar

    refresh_secs = cache_manager.get_refresh_seconds("usdmxn", settings)
    state = get_market_state(
        calendar=_market_calendar(settings), refresh_seconds=refresh_secs
    )
    summary["market_status"] = state.market_status

    # Always score due prior recommendations first (read-only; no quota).
    try:
        ev = evaluate_due(db, now=now)
        summary["evaluated_outcomes_count"] = int(ev.get("evaluated", 0))
    except Exception:  # noqa: BLE001
        logger.exception("evaluate_due failed during hourly job")
        db.rollback()

    # Market closed (weekend/holiday): never fetch live; never generate.
    if not state.is_open:
        summary["market_source"] = _latest_snapshot_source(db) or "none"
        summary["skipped_reason"] = (
            "weekend" if state.market_status == MarketStatus.WEEKEND else "market_closed"
        )
        _record_run(db, summary)
        return summary

    # De-dupe: at most one job recommendation per clock hour / pair / model.
    if _existing_this_hour(db, PAIR, MODEL_VERSION, now) is not None:
        summary["market_source"] = _latest_snapshot_source(db) or "cached"
        summary["skipped_reason"] = "duplicate_this_hour"
        _record_run(db, summary)
        return summary

    # Generate via the same pipeline as /analysis/usdmxn (one fetch at most,
    # respecting refresh limits; stores a paper recommendation when tradeable
    # data exists).
    from app.routers.analysis import analyze_usdmxn

    before_id = (_latest_reco(db, PAIR) or None)
    before_id = before_id.id if before_id else 0
    try:
        payload = analyze_usdmxn(db)
    except Exception:  # noqa: BLE001
        logger.exception("hourly analysis failed")
        db.rollback()
        summary["skipped_reason"] = "analysis_error"
        _record_run(db, summary)
        return summary

    market = payload.get("market") or {}
    summary["market_source"] = market.get("source")

    if payload.get("market_data_unavailable"):
        summary["market_source"] = market.get("source") or "unavailable"
        summary["skipped_reason"] = "market_data_unavailable"
        _record_run(db, summary)
        return summary

    after = _latest_reco(db, PAIR)
    if after and after.id != before_id:
        summary["created_recommendation"] = True
        summary["recommendation_id"] = after.id
    else:
        summary["skipped_reason"] = "no_recommendation_stored"

    _record_run(db, summary)
    return summary


def _serialize_run(run: Optional[JobRun]) -> Optional[dict]:
    if run is None:
        return None
    return {
        "ran_at": run.ran_at.isoformat() if run.ran_at else None,
        "job_name": run.job_name,
        "created_recommendation": run.created_recommendation,
        "recommendation_id": run.recommendation_id,
        "market_status": run.market_status,
        "market_source": run.market_source,
        "evaluated_outcomes_count": run.evaluated_outcomes_count,
        "skipped_reason": run.skipped_reason,
    }


def job_status(db: Session, now: Optional[datetime] = None) -> dict:
    """Read-only scheduler status for the dashboard."""
    now = _aware(now or datetime.now(timezone.utc))
    last_run = db.execute(
        select(JobRun).order_by(JobRun.ran_at.desc()).limit(1)
    ).scalars().first()
    last_reco = db.execute(
        select(Recommendation).order_by(Recommendation.created_at.desc()).limit(1)
    ).scalars().first()
    next_run = _hour_bucket(now) + timedelta(hours=1)
    return {
        "job": JOB_NAME,
        "schedule": "hourly",
        "schedule_cron": SCHEDULE_CRON,
        "last_scheduled_run": _serialize_run(last_run),
        "last_recommendation_at": (
            last_reco.created_at.isoformat()
            if last_reco and last_reco.created_at else None
        ),
        "next_expected_run": next_run.isoformat(),
    }
