"""Admin FIX endpoints (Phase 1 — diagnostics only, no trading)."""

from __future__ import annotations

from fastapi import APIRouter

from app.services.fix.provider import get_fix_diagnostics

router = APIRouter(prefix="/admin/research/snapshots", tags=["admin-fix"])


@router.get("/fix-status")
def fix_status() -> dict:
    """Safe FIX MD status for operators (passwords never returned)."""
    return get_fix_diagnostics()
