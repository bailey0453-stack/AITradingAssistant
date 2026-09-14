"""Scheduled USD/MXN analysis jobs.

The fast intraday job records market snapshots and paper recommendations every
five minutes while FX is open so 5m/15m/30m/60m tactical momentum can be
measured. The legacy hourly endpoint remains for compatibility and heavier
research-repair work.

Safety rules:
- never fetch gated market data while FX is closed;
- de-duplicate recommendations inside the requested cadence bucket;
- never turn unavailable/fabricated data into an actionable recommendation;
- always evaluate due paper outcomes, even when generation is skipped.
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
INTRADAY_JOB_NAME = "intraday-usdmxn-analysis"
PAIR = "USDMXN"
SCHEDULE_CRON = "0 * * * *"
INTRADAY_SCHEDULE_CRON = "*/5 * * * *"


def _aware(dt: datetime) -> datetime:
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _bucket(dt: datetime, minutes: int) -> datetime:
    dt = _aware(dt)
    minute = (dt.minute // minutes) * minutes
    return dt.replace(minute=minute, second=0, microsecond=0)


def _latest_reco(db: Session, pair: str) -> Optional[Recommendation]:
    return db.execute(
        select(Recommendation)
        .where(Recommendation.pair == pair)
        .order_by(Recommendation.id.desc())
        .limit(1)
    ).scalars().first()


def _existing_in_bucket(
    db: Session, pair: str, model_version: str, now: datetime, bucket_minutes: int
) -> Optional[int]:
    """Return a recommendation already stored in the cadence bucket."""
    target = _bucket(now, bucket_minutes)
    rows = db.execute(
        select(Recommendation)
        .where(Recommendation.pair == pair)
        .where(Recommendation.model_version == model_version)
        .order_by(Recommendation.created_at.desc())
        .limit(max(24, int(180 / bucket_minutes)))
    ).scalars().all()
    for reco in rows:
        if reco.created_at and _bucket(reco.created_at, bucket_minutes) == target:
            return reco.id
    return None


def _latest_snapshot_source(db: Session) -> Optional[str]:
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
    except Exception:  # noqa: BLE001
        logger.exception("Failed to persist JobRun")
        db.rollback()


def _run_usdmxn_job(
    db: Session,
    settings: Settings,
    *,
    now: datetime,
    job_name: str,
    bucket_minutes: int,
) -> dict:
    summary: dict = {
        "job": job_name,
        "cadence_minutes": bucket_minutes,
        "ran_at": now.isoformat(),
        "created_recommendation": False,
        "recommendation_id": None,
        "market_status": None,
        "market_source": None,
        "evaluated_outcomes_count": 0,
        "skipped_reason": None,
    }

    from app.routers.market import _market_calendar

    refresh_secs = cache_manager.get_refresh_seconds("usdmxn", settings)
    state = get_market_state(
        calendar=_market_calendar(settings), refresh_seconds=refresh_secs
    )
    summary["market_status"] = state.market_status

    try:
        ev = evaluate_due(db, now=now)
        summary["evaluated_outcomes_count"] = int(ev.get("evaluated", 0))
    except Exception:  # noqa: BLE001
        logger.exception("evaluate_due failed during %s", job_name)
        db.rollback()

    if not state.is_open:
        summary["market_source"] = _latest_snapshot_source(db) or "none"
        summary["skipped_reason"] = (
            "weekend" if state.market_status == MarketStatus.WEEKEND else "market_closed"
        )
        _record_run(db, summary)
        return summary

    if _existing_in_bucket(db, PAIR, MODEL_VERSION, now, bucket_minutes) is not None:
        summary["market_source"] = _latest_snapshot_source(db) or "cached"
        summary["skipped_reason"] = "duplicate_cadence_bucket"
        _record_run(db, summary)
        return summary

    from app.routers.analysis import analyze_usdmxn

    before = _latest_reco(db, PAIR)
    before_id = before.id if before else 0
    try:
        payload = analyze_usdmxn(db)
    except Exception:  # noqa: BLE001
        logger.exception("%s analysis failed", job_name)
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

    momentum = ((payload.get("signal_breakdown") or {}).get("intraday_momentum") or {})
    summary["momentum_windows"] = momentum.get("windows") or {}
    summary["tactical_momentum_score"] = momentum.get("tactical_score")
    _record_run(db, summary)
    return summary


def run_intraday_usdmxn_job(
    db: Session, settings: Optional[Settings] = None, *, now: Optional[datetime] = None
) -> dict:
    """Generate tactical snapshots/recommendations every five minutes."""
    settings = settings or get_settings()
    return _run_usdmxn_job(
        db,
        settings,
        now=_aware(now or datetime.now(timezone.utc)),
        job_name=INTRADAY_JOB_NAME,
        bucket_minutes=5,
    )


def run_hourly_usdmxn_job(
    db: Session, settings: Optional[Settings] = None, *, now: Optional[datetime] = None
) -> dict:
    """Compatibility hourly generation path.

    The five-minute job normally creates a row at the top of the hour first, so
    this call safely de-duplicates against the same hourly bucket.
    """
    settings = settings or get_settings()
    return _run_usdmxn_job(
        db,
        settings,
        now=_aware(now or datetime.now(timezone.utc)),
        job_name=JOB_NAME,
        bucket_minutes=60,
    )


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
    next_fast = _bucket(now, 5) + timedelta(minutes=5)
    return {
        "job": INTRADAY_JOB_NAME,
        "schedule": "every 5 minutes",
        "schedule_cron": INTRADAY_SCHEDULE_CRON,
        "hourly_compatibility_cron": SCHEDULE_CRON,
        "last_scheduled_run": _serialize_run(last_run),
        "last_recommendation_at": (
            last_reco.created_at.isoformat()
            if last_reco and last_reco.created_at else None
        ),
        "next_expected_run": next_fast.isoformat(),
    }
