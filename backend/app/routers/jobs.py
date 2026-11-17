"""Scheduled (cron) job endpoints."""

from __future__ import annotations

import hmac
import logging
import os

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.orm import Session

from app.config import get_settings
from app.database import get_db
from app.services.cftc_positioning import repair_cftc_positioning
from app.services.fx_options_repair import repair_fx_options
from app.services.intraday_repair import repair_intraday_usdmxn
from app.services.mexico_yield_repair import repair_mexico_yields
from app.services.rate_repair import repair_policy_rates
from app.services.scheduled_jobs import (
    job_status,
    run_hourly_usdmxn_job,
    run_intraday_usdmxn_job,
)
from app.services.research_import_service import cron_daily_research_update, cron_research_import_continue

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


def _safe_repair(db: Session, label: str, fn) -> dict:
    try:
        return fn(db)
    except Exception as exc:  # noqa: BLE001
        logger.exception("%s repair failed during hourly job", label)
        db.rollback()
        return {"ok": False, "reason": str(exc)}


@router.api_route("/intraday-usdmxn-analysis", methods=["POST", "GET"], dependencies=[Depends(require_cron_auth)])
def intraday_usdmxn_analysis(db: Session = Depends(get_db)) -> dict:
    """Generate a fresh tactical USD/MXN snapshot/recommendation every five minutes."""
    return run_intraday_usdmxn_job(db)


@router.api_route("/news-watch", methods=["POST", "GET"], dependencies=[Depends(require_cron_auth)])
def news_watch(db: Session = Depends(get_db)) -> dict:
    """Watch real headlines independently of whether a dashboard is open."""
    from app.services.short_term_news import snapshot
    result = snapshot(db)
    try:
        from app.services.fix.provider import get_fix_quote
        from app.services.personal_exit_watch import background
        from app.services.market_hours import get_market_state
        from app.routers.decision import _short_term_calendar
        from time import time
        payload={**_short_term_calendar(int(time()//300)),"news_watch":result,
                 "market_state":{"is_open":get_market_state().is_open}}
        position_status=background(db,get_fix_quote(),payload)
    except Exception:
        db.rollback()
        logger.exception("Personal exit watch failed; no exit order is sent.")
        position_status={"status":"UNAVAILABLE"}
    from app.routers.decision import release_watch_now
    try:
        release=release_watch_now(db)
        release_status={k:release.get(k) for k in ("phase","feed_status","provider","error")}
        if release.get("error"):
            logger.warning("Release watch: %s: %s",release.get("feed_status"),release["error"])
    except Exception:
        db.rollback()
        logger.exception("Release watch failed; no release action is issued.")
        release_status={"phase":"UNAVAILABLE"}
    return {k: v for k, v in result.items() if k != "items"} | {"headline_count": len(result.get("items") or []),"release_watch":release_status,"exit_watch":position_status}


@router.api_route("/hourly-usdmxn-analysis", methods=["POST", "GET"], dependencies=[Depends(require_cron_auth)])
def hourly_usdmxn_analysis(db: Session = Depends(get_db)) -> dict:
    """Refresh heavier research inputs, then run the compatibility hourly job."""
    repairs = {
        "policy_rate_repair": _safe_repair(db, "Policy-rate", repair_policy_rates),
        "mexico_yield_repair": _safe_repair(db, "Mexico-yield", repair_mexico_yields),
        "intraday_repair": _safe_repair(db, "Intraday USD/MXN", repair_intraday_usdmxn),
        "fx_options_repair": _safe_repair(db, "USD/MXN options", repair_fx_options),
        "cftc_positioning_repair": _safe_repair(db, "CFTC Mexican peso positioning", repair_cftc_positioning),
    }
    summary = run_hourly_usdmxn_job(db)
    summary.update(repairs)
    return summary


@router.get("/status")
def jobs_status(db: Session = Depends(get_db)) -> dict:
    return job_status(db)


@router.api_route("/research-import-continue", methods=["POST", "GET"], dependencies=[Depends(require_cron_auth)])
def research_import_continue(db: Session = Depends(get_db)) -> dict:
    return cron_research_import_continue(db)


@router.api_route("/daily-research-update", methods=["POST", "GET"], dependencies=[Depends(require_cron_auth)])
def daily_research_update(db: Session = Depends(get_db)) -> dict:
    return cron_daily_research_update(db)
