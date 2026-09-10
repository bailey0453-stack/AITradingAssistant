"""FIX market-data and trading provider facade with safe diagnostics."""

from __future__ import annotations

import os
from datetime import datetime, timezone
from typing import Any

import httpx

from app.config import Settings, get_settings
from app.services.fix.centroid_md_session import (
    get_centroid_md_session,
    start_centroid_md_background,
    stop_centroid_md_background,
)
from app.services.fix.quote_store import FixQuoteStore
from app.services.fix.simulation import SimulatedOrder
from app.services.fix.trading_session import (
    get_centroid_trading_session,
    start_centroid_trading_background,
    stop_centroid_trading_background,
)
from app.services.secrets import scrub

_HEARTBEAT_STALE_SECONDS = 90
_RAILWAY_FIX_WORKER = "https://centroid-fix-worker-production.up.railway.app"


def _scrub_text(text: str | None, settings: Settings) -> str | None:
    if not text:
        return text
    return scrub(
        text,
        settings.centroid_md_password,
        settings.centroid_md_username,
        settings.centroid_td_password,
        settings.centroid_td_username,
    )


def _format_reject_display(last_inbound: dict[str, Any], requested_symbol: str | None) -> str | None:
    text = last_inbound.get("text") or last_inbound.get("raw_reject_text")
    if not text:
        return None
    sym = requested_symbol or "?"
    if "invalid symbol" in text.lower():
        return f"Market data request rejected: Invalid Symbol ({sym})"
    msg_type = last_inbound.get("msg_type")
    label = last_inbound.get("msg_type_label") or msg_type
    if msg_type in {"Y", "j", "3"}:
        return f"Market data request rejected ({label}): {text}"
    return text


def _worker_base_url(settings: Settings) -> str | None:
    # Only Vercel proxies to the long-running Railway worker. Railway/local
    # processes own the persistent TCP sessions directly; this also prevents a
    # worker from accidentally proxying to itself.
    if not os.getenv("VERCEL"):
        return None
    return (settings.fix_worker_base_url or _RAILWAY_FIX_WORKER).rstrip("/")


def _worker_admin_secret(settings: Settings) -> str | None:
    return os.getenv("ADMIN_SECRET") or os.getenv("CRON_SECRET") or settings.cron_secret


def _remote_worker_request(
    settings: Settings,
    method: str,
    path: str,
    *,
    json: dict[str, Any] | None = None,
    admin: bool = False,
) -> dict[str, Any] | None:
    base = _worker_base_url(settings)
    if not base:
        return None
    headers: dict[str, str] = {}
    if admin:
        secret = _worker_admin_secret(settings)
        if not secret:
            raise RuntimeError("FIX worker admin secret is not configured")
        headers["Authorization"] = f"Bearer {secret}"
    try:
        response = httpx.request(
            method,
            base + path,
            json=json,
            headers=headers,
            timeout=min(settings.http_timeout_seconds, 8.0),
        )
        if response.status_code >= 400:
            detail = None
            try:
                payload = response.json()
                detail = payload.get("detail") if isinstance(payload, dict) else None
            except Exception:
                detail = None
            raise RuntimeError(
                _scrub_text(str(detail or f"FIX worker returned HTTP {response.status_code}"), settings)
            )
        payload = response.json()
        return payload if isinstance(payload, dict) else None
    except httpx.HTTPError as exc:
        raise RuntimeError(_scrub_text(f"FIX worker request failed: {exc}", settings)) from exc


def _remote_worker_status(settings: Settings) -> dict[str, Any] | None:
    try:
        return _remote_worker_request(
            settings,
            "GET",
            "/admin/research/snapshots/fix-status",
        )
    except RuntimeError:
        return None


def get_fix_quote(symbol: str | None = None, settings: Settings | None = None) -> dict | None:
    settings = settings or get_settings()
    remote = _remote_worker_status(settings)
    if remote and isinstance(remote.get("quote"), dict):
        return remote["quote"]
    sym = symbol or settings.centroid_md_symbol_usdmxn or "USD/MXN"
    quote = FixQuoteStore.get().get_quote(sym)
    return quote.to_dict() if quote else None


def request_fix_security_discovery(settings: Settings | None = None) -> dict[str, Any]:
    settings = settings or get_settings()
    session = get_centroid_md_session(settings)
    return session.request_security_list()


def get_fix_diagnostics(settings: Settings | None = None) -> dict[str, Any]:
    settings = settings or get_settings()
    remote = _remote_worker_status(settings)
    if remote:
        remote["remote_worker"] = True
        return remote
    store = FixQuoteStore.get()
    sym = settings.centroid_md_symbol_usdmxn or "USD/MXN"
    payload = store.diagnostics(primary_symbol=sym)
    session = dict(payload.get("session") or {})
    last_md = dict(payload.get("last_md_request") or {})
    last_inbound = dict(payload.get("last_inbound") or {})
    discovery = dict(payload.get("security_discovery") or {})
    if session.get("last_error"):
        session["last_error"] = _scrub_text(session["last_error"], settings)
    session["warnings"] = [_scrub_text(w, settings) or w for w in (session.get("warnings") or [])]
    for key in ("text", "raw_reject_text", "md_req_reject_reason"):
        if last_inbound.get(key):
            last_inbound[key] = _scrub_text(last_inbound[key], settings)
    if discovery.get("error"):
        discovery["error"] = _scrub_text(discovery["error"], settings)
    now = datetime.now(timezone.utc)
    hb_at = session.get("last_heartbeat_at")
    heartbeat_ok = False
    if hb_at:
        try:
            heartbeat_ok = (now - datetime.fromisoformat(hb_at.replace("Z", "+00:00"))).total_seconds() <= _HEARTBEAT_STALE_SECONDS
        except ValueError:
            pass
    md_status = session.get("md_subscription_status") or "none"
    requested_symbol = last_md.get("symbol") or sym
    payload["connection"] = {
        "status": session.get("status"),
        "tcp_connected": bool(session.get("tcp_connected")),
        "fix_logged_on": bool(session.get("fix_logged_on")),
        "logon_accepted_at": session.get("last_logon_at"),
        "sender_comp_id": session.get("sender_comp_id"),
        "target_comp_id": session.get("target_comp_id"),
        "host": session.get("host"),
        "port": session.get("port"),
        "ssl_enabled": bool(session.get("ssl_enabled")),
        "outbound_seq": session.get("outbound_seq"),
        "inbound_seq": session.get("inbound_seq"),
        "heartbeat_ok": heartbeat_ok,
        "last_heartbeat_at": session.get("last_heartbeat_at"),
    }
    payload["market_data_subscription"] = {
        "status": md_status,
        "requested_symbol": requested_symbol,
        "active": md_status == "accepted",
        "reject_display": _format_reject_display(last_inbound, requested_symbol) if md_status == "rejected" else None,
    }
    payload["security_discovery"] = discovery
    payload["last_md_request"] = last_md
    payload["last_inbound"] = last_inbound
    payload["session"] = session
    payload["configured"] = settings.centroid_md_enabled
    payload["config_complete"] = settings.centroid_md_configured
    payload["configured_symbol_env"] = sym
    payload["phase"] = "1_read_only_market_data"
    payload["trading_enabled"] = settings.centroid_td_enabled
    payload["simulation_only"] = not settings.centroid_td_enabled
    payload["credentials"] = {
        "md_host_set": bool(settings.centroid_md_host),
        "md_username_set": bool(settings.centroid_md_username),
        "md_password_set": bool(settings.centroid_md_password),
        "md_sender_comp_id": settings.centroid_md_sender_comp_id,
        "md_target_comp_id": settings.centroid_md_target_comp_id,
    }
    payload["md_request_config"] = {
        "subscription_request_type": str(settings.centroid_md_subscription_request_type),
        "market_depth": str(settings.centroid_md_market_depth),
        "include_md_update_type": bool(settings.centroid_md_include_md_update_type),
    }
    return payload


def get_trading_diagnostics(settings: Settings | None = None) -> dict[str, Any]:
    settings = settings or get_settings()
    remote = _remote_worker_request(
        settings,
        "GET",
        "/admin/research/snapshots/trading-status",
        admin=True,
    )
    if remote is not None:
        remote["remote_worker"] = True
        return remote
    return get_centroid_trading_session(settings).diagnostics()


def send_trading_conformance_order(
    order: dict[str, Any],
    settings: Settings | None = None,
) -> dict[str, Any]:
    settings = settings or get_settings()
    remote = _remote_worker_request(
        settings,
        "POST",
        "/admin/research/snapshots/trading-conformance/order",
        json=order,
        admin=True,
    )
    if remote is not None:
        remote["remote_worker"] = True
        return remote
    session = get_centroid_trading_session(settings)
    sent = session.send_conformance_order(**order)
    return {"sent": True, "mode": "conformance", "order": sent, "session": session.diagnostics()}


def ensure_fix_session_started(settings: Settings | None = None) -> None:
    settings = settings or get_settings()
    if os.getenv("VERCEL"):
        return
    if settings.centroid_md_enabled:
        start_centroid_md_background(settings)
    if settings.centroid_td_enabled and settings.centroid_td_configured:
        start_centroid_trading_background(settings)


def stop_fix_session(settings: Settings | None = None) -> None:
    settings = settings or get_settings()
    if os.getenv("VERCEL"):
        return
    stop_centroid_md_background(settings)
    stop_centroid_trading_background(settings)


__all__ = [
    "SimulatedOrder",
    "ensure_fix_session_started",
    "get_fix_diagnostics",
    "get_fix_quote",
    "get_trading_diagnostics",
    "request_fix_security_discovery",
    "send_trading_conformance_order",
    "stop_fix_session",
]
