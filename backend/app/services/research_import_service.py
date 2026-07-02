"""Orchestrate historical research imports from admin UI and cron (staged, resumable)."""

from __future__ import annotations

import logging
import uuid
from datetime import date, datetime, timedelta, timezone
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.models import HistoricalEvent, HistoricalMarketSnapshot, ResearchMarketSnapshot
from app.models.research_import import HistoricalImportJob
from app.services.history.importers import (
    AlphaVantageImporter,
    FREDEconomicEventsImporter,
    FREDImporter,
    SERIES_COLUMN,
    YahooFinanceImporter,
)
from app.services.history.snapshot_builder import build_research_snapshots

logger = logging.getLogger(__name__)

FULL_STAGES: tuple[str, ...] = ("yahoo", "fred", "alphavantage", "events", "snapshots")
INCREMENTAL_STAGES: tuple[str, ...] = ("yahoo", "fred", "alphavantage", "events", "snapshots")

STAGE_LABELS: dict[str, str] = {
    "yahoo": "Yahoo Finance",
    "fred": "FRED macro series",
    "alphavantage": "Alpha Vantage",
    "events": "Economic events (FRED)",
    "snapshots": "Research snapshot builder",
}

MIN_SNAPSHOTS_FOR_DUPLICATE_BLOCK = 100
OVERLAP_DAYS = 14
STALE_JOB_MINUTES = 45


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _as_date(ts: datetime) -> date:
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return ts.date()


def _stages_for_mode(mode: str) -> tuple[str, ...]:
    return FULL_STAGES if mode == "full" else INCREMENTAL_STAGES


def job_to_dict(job: HistoricalImportJob | None) -> dict | None:
    if job is None:
        return None
    return {
        "job_uuid": job.job_uuid,
        "mode": job.mode,
        "status": job.status,
        "importer": job.importer,
        "current_stage": job.current_stage,
        "current_importer": STAGE_LABELS.get(job.current_stage or "", job.current_stage),
        "stages_completed": list(job.stages_completed or []),
        "stages_skipped": list(job.stages_skipped or []),
        "progress_pct": round(float(job.progress_pct or 0), 1),
        "lookback_days": job.lookback_days,
        "since_date": job.since_date.isoformat() if job.since_date else None,
        "series_points": int(job.series_points or 0),
        "events_imported": int(job.events_imported or 0),
        "reactions_imported": int(job.reactions_imported or 0),
        "snapshots_built": int(job.snapshots_built or 0),
        "errors": list(job.errors or []),
        "stage_log": list(job.stage_log or []),
        "summary": job.summary,
        "message": job.message,
        "started_at": job.created_at.isoformat() if job.created_at else None,
        "updated_at": job.updated_at.isoformat() if job.updated_at else None,
        "completed_at": job.completed_at.isoformat() if job.completed_at else None,
    }


def _research_imported_count(db: Session) -> int:
    return int(
        db.execute(
            select(func.count(ResearchMarketSnapshot.id)).where(
                ResearchMarketSnapshot.source_quality != "sample"
            )
        ).scalar()
        or 0
    )


def _latest_snapshot_date(db: Session) -> date | None:
    val = db.execute(select(func.max(ResearchMarketSnapshot.trade_date))).scalar()
    return val if isinstance(val, date) else None


def get_active_job(db: Session) -> HistoricalImportJob | None:
    return db.execute(
        select(HistoricalImportJob)
        .where(HistoricalImportJob.status == "running")
        .order_by(HistoricalImportJob.updated_at.desc())
    ).scalars().first()


def get_job_by_uuid(db: Session, job_uuid: str) -> HistoricalImportJob | None:
    return db.execute(
        select(HistoricalImportJob).where(HistoricalImportJob.job_uuid == job_uuid)
    ).scalars().first()


def get_last_completed_job(db: Session) -> HistoricalImportJob | None:
    return db.execute(
        select(HistoricalImportJob)
        .where(HistoricalImportJob.status == "completed")
        .order_by(HistoricalImportJob.completed_at.desc())
    ).scalars().first()


def _can_start_full(db: Session, *, force: bool = False) -> dict[str, Any]:
    active = get_active_job(db)
    if active:
        return {
            "allowed": True,
            "resume": True,
            "reason": "import_in_progress",
            "job_uuid": active.job_uuid,
        }
    imported = _research_imported_count(db)
    if imported >= MIN_SNAPSHOTS_FOR_DUPLICATE_BLOCK and not force:
        last = get_last_completed_job(db)
        return {
            "allowed": False,
            "resume": False,
            "reason": "already_imported",
            "snapshots": imported,
            "last_completed_at": last.completed_at.isoformat() if last and last.completed_at else None,
            "hint": "Use force=true to run a full re-import, or run Daily Incremental Update.",
        }
    return {"allowed": True, "resume": False, "reason": "ready"}


def _can_start_incremental(db: Session) -> dict[str, Any]:
    active = get_active_job(db)
    if active:
        return {
            "allowed": True,
            "resume": True,
            "reason": "import_in_progress",
            "job_uuid": active.job_uuid,
        }
    latest = _latest_snapshot_date(db)
    if not latest:
        return {
            "allowed": True,
            "resume": False,
            "reason": "empty_database",
            "hint": "No snapshots yet — a full import will run instead.",
        }
    return {
        "allowed": True,
        "resume": False,
        "reason": "ready",
        "latest_snapshot": latest.isoformat(),
    }


def get_import_overview(db: Session) -> dict:
    """Public read-only import status for dashboard polling."""
    from app.services.admin_auth import _configured_secret

    active = get_active_job(db)
    last = get_last_completed_job(db)
    return {
        "active_job": job_to_dict(active),
        "last_completed_job": job_to_dict(last),
        "can_start_full": _can_start_full(db),
        "can_start_incremental": _can_start_incremental(db),
        "research_snapshots": _research_imported_count(db),
        "latest_snapshot_date": (
            _latest_snapshot_date(db).isoformat() if _latest_snapshot_date(db) else None
        ),
        "auth_configured": bool(_configured_secret()),
        "stage_labels": STAGE_LABELS,
    }


def _append_log(job: HistoricalImportJob, entry: dict) -> None:
    log = list(job.stage_log or [])
    entry = {**entry, "at": _utcnow().isoformat()}
    log.append(entry)
    job.stage_log = log


def _append_error(job: HistoricalImportJob, message: str) -> None:
    errs = list(job.errors or [])
    errs.append(message)
    job.errors = errs


def _update_progress(job: HistoricalImportJob) -> None:
    stages = _stages_for_mode(job.mode)
    done = len(set(job.stages_completed or []) | set(job.stages_skipped or []))
    job.progress_pct = min(99.0, (done / len(stages)) * 100) if stages else 0.0


def _next_stage(job: HistoricalImportJob) -> str | None:
    stages = _stages_for_mode(job.mode)
    finished = set(job.stages_completed or []) | set(job.stages_skipped or [])
    for stage in stages:
        if stage not in finished:
            return stage
    return None


def persist_series_bars(
    db: Session,
    bars: list[dict],
    *,
    source: str,
    source_quality: str,
    since_date: date | None = None,
) -> int:
    """Insert raw series bars, skipping duplicates on (series, calendar day)."""
    if since_date:
        bars = [b for b in bars if _as_date(b["ts"]) >= since_date]
    if not bars:
        return 0

    series_names = {b.get("series", "USDMXN") for b in bars}
    min_ts = min(b["ts"] for b in bars)
    max_ts = max(b["ts"] for b in bars)
    if min_ts.tzinfo is None:
        min_ts = min_ts.replace(tzinfo=timezone.utc)
    if max_ts.tzinfo is None:
        max_ts = max_ts.replace(tzinfo=timezone.utc)

    existing: set[tuple[str, date]] = set()
    rows = db.execute(
        select(HistoricalMarketSnapshot.series, HistoricalMarketSnapshot.ts).where(
            HistoricalMarketSnapshot.series.in_(series_names),
            HistoricalMarketSnapshot.ts >= min_ts - timedelta(days=1),
            HistoricalMarketSnapshot.ts <= max_ts + timedelta(days=1),
        )
    ).all()
    for series, ts in rows:
        existing.add((series, _as_date(ts)))

    added = 0
    for bar in bars:
        series = bar.get("series", "USDMXN")
        ts = bar["ts"]
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        key = (series, _as_date(ts))
        if key in existing:
            continue
        column = SERIES_COLUMN.get(series)
        fields = {
            "usdmxn": bar.get("usdmxn"),
            "dxy": bar.get("dxy"),
            "us2y": bar.get("us2y"),
            "us10y": bar.get("us10y"),
            "oil": bar.get("oil"),
            "gold": bar.get("gold"),
            "vix": bar.get("vix"),
            "sp_futures": bar.get("sp_futures"),
        }
        value = bar.get("value")
        if column and value is not None and fields.get(column) is None:
            fields[column] = value
        db.add(
            HistoricalMarketSnapshot(
                series=series,
                ts=ts,
                regime=bar.get("regime"),
                source=source,
                source_quality=source_quality,
                **fields,
            )
        )
        existing.add(key)
        added += 1
    if added:
        db.commit()
    return added


def _create_job(
    db: Session,
    *,
    mode: str,
    lookback_days: int,
    since_date: date | None,
) -> HistoricalImportJob:
    job = HistoricalImportJob(
        job_uuid=str(uuid.uuid4()),
        mode=mode,
        status="running",
        importer="research",
        current_stage=_next_stage_for_new(mode),
        lookback_days=lookback_days,
        since_date=since_date,
        stages_completed=[],
        stages_skipped=[],
        errors=[],
        stage_log=[],
        message=f"Started {mode} research import.",
    )
    db.add(job)
    db.commit()
    db.refresh(job)
    return job


def _next_stage_for_new(mode: str) -> str:
    stages = _stages_for_mode(mode)
    return stages[0] if stages else "snapshots"


def start_full_import(db: Session, *, force: bool = False) -> dict:
    gate = _can_start_full(db, force=force)
    if not gate.get("allowed"):
        return {"ok": False, **gate}
    active = get_active_job(db)
    if active and gate.get("resume"):
        return {
            "ok": True,
            "resumed": True,
            "job": job_to_dict(active),
            "message": "Resuming in-progress full import.",
        }

    settings = get_settings()
    lookback = getattr(settings, "history_lookback_days", 3650)
    job = _create_job(db, mode="full", lookback_days=lookback, since_date=None)
    result = run_import_step(db, job.job_uuid)
    return {"ok": True, "started": True, "job": result.get("job"), "step": result}


def start_incremental_import(db: Session) -> dict:
    gate = _can_start_incremental(db)
    active = get_active_job(db)
    if active and gate.get("resume"):
        return {
            "ok": True,
            "resumed": True,
            "job": job_to_dict(active),
            "message": "Resuming in-progress import.",
        }

    settings = get_settings()
    latest = _latest_snapshot_date(db)
    if not latest:
        return start_full_import(db, force=False)

    since = latest - timedelta(days=OVERLAP_DAYS)
    lookback = max(30, (date.today() - since).days + 5)
    job = _create_job(db, mode="incremental", lookback_days=lookback, since_date=since)
    result = run_import_step(db, job.job_uuid)
    return {
        "ok": True,
        "started": True,
        "since_date": since.isoformat(),
        "job": result.get("job"),
        "step": result,
    }


def run_import_step(db: Session, job_uuid: str) -> dict:
    """Execute exactly one import stage (serverless-safe)."""
    job = get_job_by_uuid(db, job_uuid)
    if not job:
        return {"ok": False, "error": "job_not_found"}
    if job.status != "running":
        return {"ok": False, "error": "job_not_running", "job": job_to_dict(job)}

    stage = _next_stage(job)
    if not stage:
        return _complete_job(db, job)

    job.current_stage = stage
    job.updated_at = _utcnow()
    db.commit()

    settings = get_settings()
    stage_result: dict[str, Any] = {"stage": stage, "status": "ok"}

    try:
        if stage == "yahoo":
            imp = YahooFinanceImporter(settings, lookback_days=job.lookback_days)
            added = persist_series_bars(
                db,
                imp.fetch_series(),
                source=imp.name,
                source_quality=imp.source_quality,
                since_date=job.since_date,
            )
            job.series_points = int(job.series_points or 0) + added
            stage_result["series_points"] = added

        elif stage == "fred":
            imp = FREDImporter(settings, lookback_days=job.lookback_days)
            added = persist_series_bars(
                db,
                imp.fetch_series(),
                source=imp.name,
                source_quality=imp.source_quality,
                since_date=job.since_date,
            )
            job.series_points = int(job.series_points or 0) + added
            stage_result["series_points"] = added

        elif stage == "alphavantage":
            if not getattr(settings, "alpha_vantage_api_key", None):
                skipped = list(job.stages_skipped or [])
                skipped.append(stage)
                job.stages_skipped = skipped
                stage_result = {"stage": stage, "status": "skipped", "reason": "no API key"}
            else:
                imp = AlphaVantageImporter(settings, outputsize="full")
                imp.throttle = True
                added = persist_series_bars(
                    db,
                    imp.fetch_series(),
                    source=imp.name,
                    source_quality=imp.source_quality,
                    since_date=job.since_date,
                )
                job.series_points = int(job.series_points or 0) + added
                stage_result["series_points"] = added

        elif stage == "events":
            imp = FREDEconomicEventsImporter(settings, lookback_days=job.lookback_days)
            r = imp.run(db, since_date=job.since_date, skip_duplicates=True)
            job.events_imported = int(job.events_imported or 0) + int(r.get("events", 0))
            job.reactions_imported = int(job.reactions_imported or 0) + int(r.get("reactions", 0))
            stage_result.update(r)

        elif stage == "snapshots":
            min_date = job.since_date
            if job.mode == "full" and not min_date:
                min_date = date.today().replace(year=date.today().year - 10)
            built = build_research_snapshots(
                db,
                min_date=min_date,
                replace=False,
                source="research",
                source_quality="imported",
            )
            created = int(built.get("snapshots", 0))
            job.snapshots_built = int(job.snapshots_built or 0) + created
            stage_result.update(built)

        else:
            stage_result = {"stage": stage, "status": "skipped", "reason": "unknown stage"}
            skipped = list(job.stages_skipped or [])
            skipped.append(stage)
            job.stages_skipped = skipped

    except Exception as exc:  # noqa: BLE001 - stage failure should not crash API
        db.rollback()
        msg = f"{stage}: {exc}"
        logger.exception("Research import stage failed: %s", msg)
        _append_error(job, msg)
        stage_result = {"stage": stage, "status": "error", "error": str(exc)}
        job.status = "failed"
        job.message = f"Import failed at stage {STAGE_LABELS.get(stage, stage)}."
        job.updated_at = _utcnow()
        db.commit()
        db.refresh(job)
        return {"ok": False, "job": job_to_dict(job), "stage_result": stage_result}

    if stage_result.get("status") != "skipped":
        completed = list(job.stages_completed or [])
        if stage not in completed:
            completed.append(stage)
        job.stages_completed = completed

    _append_log(job, stage_result)
    _update_progress(job)
    job.updated_at = _utcnow()
    db.commit()
    db.refresh(job)

    if _next_stage(job) is None:
        return _complete_job(db, job)

    job.message = f"Completed {STAGE_LABELS.get(stage, stage)} — continuing…"
    db.commit()
    db.refresh(job)
    return {"ok": True, "job": job_to_dict(job), "stage_result": stage_result}


def _complete_job(db: Session, job: HistoricalImportJob) -> dict:
    job.status = "completed"
    job.progress_pct = 100.0
    job.completed_at = _utcnow()
    job.current_stage = None
    job.summary = {
        "mode": job.mode,
        "series_points": int(job.series_points or 0),
        "events_imported": int(job.events_imported or 0),
        "reactions_imported": int(job.reactions_imported or 0),
        "snapshots_built": int(job.snapshots_built or 0),
        "stages_completed": list(job.stages_completed or []),
        "stages_skipped": list(job.stages_skipped or []),
        "errors": list(job.errors or []),
    }
    job.message = (
        f"Import complete — {job.summary['snapshots_built']} snapshot rows built, "
        f"{job.summary['series_points']} series points added."
    )
    job.updated_at = _utcnow()
    db.commit()
    db.refresh(job)
    return {"ok": True, "completed": True, "job": job_to_dict(job)}


def continue_import_steps(db: Session, job_uuid: str | None = None, *, max_steps: int = 1) -> dict:
    """Run up to ``max_steps`` stages on a running job (cron / auto-continue)."""
    job = get_job_by_uuid(db, job_uuid) if job_uuid else get_active_job(db)
    if not job:
        return {"ok": True, "idle": True, "message": "No running import job."}
    results = []
    for _ in range(max(1, max_steps)):
        if job.status != "running":
            break
        step = run_import_step(db, job.job_uuid)
        results.append(step)
        if step.get("completed") or not step.get("ok"):
            break
        job = get_job_by_uuid(db, job.job_uuid) or job
    return {
        "ok": True,
        "steps_run": len(results),
        "results": results,
        "job": job_to_dict(get_job_by_uuid(db, job.job_uuid)),
    }


def cron_research_import_continue(db: Session) -> dict:
    """Cron: advance a running import, or bootstrap full import if DB is empty."""
    active = get_active_job(db)
    if active:
        return continue_import_steps(db, active.job_uuid, max_steps=1)

    if _research_imported_count(db) == 0:
        started = start_full_import(db, force=False)
        if started.get("ok") and started.get("job"):
            uuid_ = started["job"]["job_uuid"]
            return continue_import_steps(db, uuid_, max_steps=1)
        return started

    return {"ok": True, "idle": True, "message": "Research database populated; no active import."}


def cron_daily_research_update(db: Session) -> dict:
    """Cron: start incremental update when idle (weekday after US close)."""
    active = get_active_job(db)
    if active:
        return {
            "ok": True,
            "skipped": True,
            "reason": "import_in_progress",
            "job": job_to_dict(active),
        }
    started = start_incremental_import(db)
    if started.get("ok") and started.get("job"):
        uuid_ = started["job"]["job_uuid"]
        continued = continue_import_steps(db, uuid_, max_steps=2)
        return {"started": started, "continued": continued}
    return started
