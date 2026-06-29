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
from app.services.scheduled_jobs import job_status, run_hourly_usdmxn_job

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/jobs", tags=["jobs"])


def _configured_secret() -> str | None:
    """The expected cron secret (env first — what Vercel injects — then config)."""
    return os.getenv("CRON_SECRET") or get_settings().cron_secret


def _provided_secret(request: Request) -> str | None:
    auth = request.headers.get("authorization") or ""
    if auth.lower().startswith("bearer "):
        return auth[7:].strip()
    return request.headers.get("x-cron-secret") or request.query_params.get("secret")


def require_cron_auth(request: Request) -> None:
    """Reject requests without the correct ``CRON_SECRET``.

    When no secret is configured we refuse in production (misconfiguration) but
    allow mock/dev mode so local runs and tests work without a secret.
    """
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
    """Run the hourly USD/MXN analysis job (cron-triggered or manual)."""
    return run_hourly_usdmxn_job(db)


@router.get("/status")
def jobs_status(db: Session = Depends(get_db)) -> dict:
    """Scheduler status for the dashboard (no auth; read-only, no secrets)."""
    return job_status(db)
