"""Admin FIX endpoints for market data and GFC trading conformance."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from app.config import get_settings
from app.services.admin_auth import require_admin_auth
from app.services.fix.provider import get_fix_diagnostics, request_fix_security_discovery
from app.services.fix.trading_session import get_centroid_trading_session

router = APIRouter(prefix="/admin/research/snapshots", tags=["admin-fix"])


class ConformanceOrderRequest(BaseModel):
    symbol: str = Field(min_length=1, max_length=64)
    side: str = Field(pattern="^[12]$")
    quantity: float = Field(gt=0)
    ord_type: str = Field(default="1", pattern="^[123]$")
    time_in_force: str = Field(default="3", pattern="^[134]$")
    price: float | None = Field(default=None, gt=0)
    ttl_ms: int | None = Field(default=None, gt=0, le=600000)


@router.get("/fix-status")
def fix_status() -> dict:
    """Safe FIX MD status for operators (passwords never returned)."""
    return get_fix_diagnostics()


@router.post("/fix-discover-symbols", dependencies=[Depends(require_admin_auth)])
def fix_discover_symbols() -> dict:
    """Ask the live Centroid FIX session for its entitled security list."""
    try:
        return request_fix_security_discovery()
    except ConnectionError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.get("/trading-status", dependencies=[Depends(require_admin_auth)])
def trading_status() -> dict:
    """Return scrubbed GFC trading-session diagnostics."""
    settings = get_settings()
    return get_centroid_trading_session(settings).diagnostics()


@router.post("/trading-conformance/order", dependencies=[Depends(require_admin_auth)])
def trading_conformance_order(body: ConformanceOrderRequest) -> dict:
    """Send one explicitly requested demo/conformance order only."""
    settings = get_settings()
    if not settings.centroid_td_conformance_mode:
        raise HTTPException(status_code=403, detail="GFC trading conformance mode is disabled")
    if not settings.centroid_td_enabled:
        raise HTTPException(status_code=409, detail="GFC trading session is disabled")
    if not settings.centroid_td_configured:
        raise HTTPException(status_code=409, detail="GFC trading session is not fully configured")
    if body.ord_type == "2" and body.price is None:
        raise HTTPException(status_code=422, detail="Limit orders require price")
    session = get_centroid_trading_session(settings)
    try:
        order = session.send_conformance_order(
            symbol=body.symbol,
            side=body.side,
            quantity=body.quantity,
            ord_type=body.ord_type,
            time_in_force=body.time_in_force,
            price=body.price,
            ttl_ms=body.ttl_ms,
        )
        return {"sent": True, "mode": "conformance", "order": order, "session": session.diagnostics()}
    except (ConnectionError, PermissionError, ValueError) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
