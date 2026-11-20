"""Admin endpoints for historical research database imports."""

from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.database import get_db
from app.services.admin_auth import require_admin_auth
from app.services.rate_repair import repair_policy_rates
from app.services.research_import_service import (
    continue_import_steps,
    get_active_job,
    get_import_overview,
    get_job_by_uuid,
    job_to_dict,
    rebuild_research_snapshots_from_raw,
    run_import_step,
    start_full_import,
    start_incremental_import,
)

router = APIRouter(prefix="/admin/research", tags=["admin-research"])


@router.get('/snapshots/history-status')
def history_status(db: Session = Depends(get_db)) -> dict:
    from app.services.fix_history_recorder import status
    return status(db)


@router.get('/snapshots/calendar-status')
def calendar_status() -> dict:
    from app.routers.decision import _short_term_calendar
    from time import time
    result=_short_term_calendar(int(time()//300))
    return {'data_sources':result['data_sources'],
            'coverage_gaps':result['calendar_coverage_gaps'],
            'upcoming_events':result['context']['upcoming_events']}


@router.get('/snapshots/rebound-status')
def rebound_status(db: Session = Depends(get_db)) -> dict:
    from app.services.rebound_watch import local_status
    return local_status(db)


@router.post('/rebound-alert')
def rebound_alert(request: Request, event: dict, db: Session = Depends(get_db)) -> dict:
    """Deliver a worker event to the already configured Telegram destination."""
    import hmac
    import math
    from datetime import datetime, timezone
    from app.config import get_settings
    from app.services.dip_rebound import utc
    from app.services.rebound_watch import message, persist
    from app.services.telegram_alerts import send_message

    settings = get_settings()
    expected = settings.rebound_alert_relay_secret
    provided = request.headers.get('X-Rebound-Secret', '')
    if not expected or not hmac.compare_digest(provided, expected):
        raise HTTPException(status_code=401, detail='Invalid relay credentials')
    if not settings.telegram_configured:
        raise HTTPException(status_code=503, detail='Telegram unavailable')
    try:
        if event['stage'] not in {'DIP','ENTRY','TARGET','STOP','TIMEOUT','COVERAGE_GAP'}:
            raise ValueError('stage')
        if not isinstance(event['key'], str) or not 1 <= len(event['key']) <= 110:
            raise ValueError('key')
        age = (datetime.now(timezone.utc)-utc(event['at'])).total_seconds()
        if not -2 <= age <= 30:
            raise ValueError('age')
        if event['stage']=='ENTRY':
            for key in ('entry_ask','target_bid','stop_bid','target_net_usd','stop_net_usd'):
                if not math.isfinite(float(event[key])):
                    raise ValueError('level')
            utc(event['deadline'])
        text = message(event)
        if len(text) > 3500:
            raise ValueError('size')
    except (ValueError, TypeError, KeyError, OverflowError):
        raise HTTPException(status_code=422, detail='Invalid or stale paper event')
    # Separate key works whether or not worker and web app share the database.
    delivery_event = {**event, 'key':'alert:'+event['key']}
    if not persist(db, [delivery_event]):
        return {'delivered':False, 'duplicate':True}
    return {'delivered':send_message(text), 'duplicate':False}


class FullImportRequest(BaseModel):
    force: bool = False


class RebuildSnapshotsRequest(BaseModel):
    backfill_scalars: bool = True


@router.get("/import")
def import_overview(db: Session = Depends(get_db)) -> dict:
    return get_import_overview(db)


@router.get("/import/{job_uuid}")
def import_job_detail(job_uuid: str, db: Session = Depends(get_db)) -> dict:
    job = get_job_by_uuid(db, job_uuid)
    if not job:
        raise HTTPException(status_code=404, detail="Import job not found")
    return {"job": job_to_dict(job)}


@router.post("/import/full", dependencies=[Depends(require_admin_auth)])
def import_full(body: Optional[FullImportRequest] = None, force: bool = Query(default=False), db: Session = Depends(get_db)) -> dict:
    use_force = body.force if body is not None else force
    result = start_full_import(db, force=use_force)
    if not result.get("ok"):
        raise HTTPException(status_code=409, detail=result)
    return result


@router.post("/import/incremental", dependencies=[Depends(require_admin_auth)])
def import_incremental(db: Session = Depends(get_db)) -> dict:
    return start_incremental_import(db)


@router.post("/rebuild-snapshots", dependencies=[Depends(require_admin_auth)])
def rebuild_snapshots(body: Optional[RebuildSnapshotsRequest] = None, db: Session = Depends(get_db)) -> dict:
    if get_active_job(db):
        raise HTTPException(status_code=409, detail={"reason": "import_in_progress", "hint": "Wait for the current import to finish before rebuilding snapshots."})
    backfill = body.backfill_scalars if body is not None else True
    return rebuild_research_snapshots_from_raw(db, backfill_scalars=backfill)


@router.post("/repair-policy-rates", dependencies=[Depends(require_admin_auth)])
def repair_rates(db: Session = Depends(get_db)) -> dict:
    """Backfill authentic Fed Funds and Banxico policy rates into research snapshots."""
    if get_active_job(db):
        raise HTTPException(status_code=409, detail={"reason": "import_in_progress"})
    return repair_policy_rates(db)


@router.post("/import/{job_uuid}/step", dependencies=[Depends(require_admin_auth)])
def import_step(job_uuid: str, db: Session = Depends(get_db)) -> dict:
    return run_import_step(db, job_uuid)


@router.post("/import/{job_uuid}/continue", dependencies=[Depends(require_admin_auth)])
def import_continue(job_uuid: str, max_steps: int = Query(default=1, ge=1, le=5), db: Session = Depends(get_db)) -> dict:
    return continue_import_steps(db, job_uuid, max_steps=max_steps)
