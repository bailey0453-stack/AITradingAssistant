"""Admin endpoints for historical research database imports."""

from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.database import get_db
from app.services.admin_auth import require_admin_auth
from app.services.research_import_service import (
    continue_import_steps,
    get_import_overview,
    get_job_by_uuid,
    job_to_dict,
    run_import_step,
    start_full_import,
    start_incremental_import,
)

router = APIRouter(prefix="/admin/research", tags=["admin-research"])


class FullImportRequest(BaseModel):
    force: bool = False


@router.get("/import")
def import_overview(db: Session = Depends(get_db)) -> dict:
    """Read-only import status (no auth — safe for dashboard polling)."""
    return get_import_overview(db)


@router.get("/import/{job_uuid}")
def import_job_detail(job_uuid: str, db: Session = Depends(get_db)) -> dict:
    job = get_job_by_uuid(db, job_uuid)
    if not job:
        raise HTTPException(status_code=404, detail="Import job not found")
    return {"job": job_to_dict(job)}


@router.post("/import/full", dependencies=[Depends(require_admin_auth)])
def import_full(
    body: Optional[FullImportRequest] = None,
    force: bool = Query(default=False),
    db: Session = Depends(get_db),
) -> dict:
    """Start or resume a full historical research import."""
    use_force = body.force if body is not None else force
    result = start_full_import(db, force=use_force)
    if not result.get("ok"):
        raise HTTPException(status_code=409, detail=result)
    return result


@router.post("/import/incremental", dependencies=[Depends(require_admin_auth)])
def import_incremental(db: Session = Depends(get_db)) -> dict:
    """Import new market data since the latest research snapshot."""
    return start_incremental_import(db)


@router.post("/import/{job_uuid}/step", dependencies=[Depends(require_admin_auth)])
def import_step(job_uuid: str, db: Session = Depends(get_db)) -> dict:
    """Advance an import job by one stage (serverless-safe)."""
    return run_import_step(db, job_uuid)


@router.post("/import/{job_uuid}/continue", dependencies=[Depends(require_admin_auth)])
def import_continue(
    job_uuid: str,
    max_steps: int = Query(default=1, ge=1, le=5),
    db: Session = Depends(get_db),
) -> dict:
    """Run multiple import stages in one request."""
    return continue_import_steps(db, job_uuid, max_steps=max_steps)
