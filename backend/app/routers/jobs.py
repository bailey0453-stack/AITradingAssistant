"""Scheduled (cron) job endpoints.

- ``POST/GET /jobs/hourly-usdmxn-analysis`` — generate + store an hourly USD/MXN
  recommendation and evaluate due prior ones. Protected by ``CRON_SECRET``.
  (Both verbs are accepted so Vercel Cron — which issues GET — can trigger it.)
- ``GET  /jobs/status`` — read-only scheduler status for the dashboard.

Auth: Vercel Cron sends ``Authorization: Bearer <CRON_SECRET>``. We also accept
an ``X-Cron-Secret`` header or a ``?secret=`` query param for manual runs.
"""

from __future__ import annotations

import hmac
import logging
import os

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.orm import Session

from app.config import get_settings
from app.database import get_db
from app.services.mexico_yield_repair import repair_mexico_yields
from app.services.rate_repair import repair_policy_rates
from app.services.scheduled_jobs import job_status, run_hourly_usdmxn_job
from app.services.research_import_service import (
    cron_daily_research_update,
    cron_research_import_continue,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/jobs", tags=["jobs"])


def _configured_secret() -> str | None:
    return os.getenv("CRON_SECRET") or get_settings().cron_secret


def _provided_secret(request: Request) -> str | None:
    auth = request.headers.get("authorization") or ""
    if auth.lower().startswith("bearer "):
        return auth[7:].strip()
    return request.headers.get("x-cron-secret") or request.query_params.get("secret")


def require_cron_auth(request: Request) -> None:
    expected = _configured_secret()
    provided = _provided_secret(request)

    if not expected:
        if get_settings().is_mock:
            return
        raise HTTPException(status_code=503, detail="CRON_SECRET not configured")

    if not provided or not hmac.compare_digest(provided, expected):
        raise HTTPException(status_code=401, detail="Invalid or missing cron secret")


@router.api_route(
    "/hourly-usdmxn-analysis",
    methods=["POST", "GET"],
    dependencies=[Depends(require_cron_auth)],
)
def hourly_usdmxn_analysis(db: Session = Depends(get_db)) -> dict:
    """Run analysis and heal relative-rate history used by similarity matching."""
    summary = run_hourly_usdmxn_job(db)
    try:
        summary["policy_rate_repair"] = repair_policy_rates(db)
    except Exception as exc:  # noqa: BLE001
        logger.exception("Policy-rate repair failed during hourly job")
        db.rollback()
        summary["policy_rate_repair"] = {"ok": False, "reason": str(exc)}
    try:
        summary["mexico_yield_repair"] = repair_mexico_yields(db)
    except Exception as exc:  # noqa: BLE001
        logger.exception("Mexico-yield repair failed during hourly job")
        db.rollback()
        summary["mexico_yield_repair"] = {"ok": False, "reason": str(exc)}
    return summary


@router.get("/status")
def jobs_status(db: Session = Depends(get_db)) -> dict:
    return job_status(db)


@router.api_route(
    "/research-import-continue",
    methods=["POST", "GET"],
    dependencies=[Depends(require_cron_auth)],
)
def research_import_continue(db: Session = Depends(get_db)) -> dict:
    return cron_research_import_continue(db)


@router.api_route(
    "/daily-research-update",
    methods=["POST", "GET"],
    dependencies=[Depends(require_cron_auth)],
)
def daily_research_update(db: Session = Depends(get_db)) -> dict:
    return cron_daily_research_update(db)
