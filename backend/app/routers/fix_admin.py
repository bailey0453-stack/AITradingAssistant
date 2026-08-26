"""Admin FIX endpoints (Phase 1 — diagnostics only, no trading)."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException

from app.services.fix.provider import get_fix_diagnostics, request_fix_security_discovery

router = APIRouter(prefix="/admin/research/snapshots", tags=["admin-fix"])


@router.get("/fix-status")
def fix_status() -> dict:
    """Safe FIX MD status for operators (passwords never returned)."""
    return get_fix_diagnostics()


@router.post("/fix-discover-symbols")
def fix_discover_symbols() -> dict:
    """Ask the live Centroid FIX session for its entitled security list."""
    try:
        return request_fix_security_discovery()
    except ConnectionError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
