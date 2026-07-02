"""FIX market-data provider facade and safe diagnostics."""

from __future__ import annotations

from typing import Any

from app.config import Settings, get_settings
from app.services.fix.centroid_md_session import (
    get_centroid_md_session,
    start_centroid_md_background,
    stop_centroid_md_background,
)
from app.services.fix.quote_store import FixQuoteStore
from app.services.fix.simulation import SimulatedOrder
from app.services.secrets import scrub


def get_fix_quote(symbol: str | None = None, settings: Settings | None = None) -> dict | None:
    settings = settings or get_settings()
    sym = symbol or settings.centroid_md_symbol_usdmxn or "USD/MXN"
    quote = FixQuoteStore.get().get_quote(sym)
    return quote.to_dict() if quote else None


def get_fix_diagnostics(settings: Settings | None = None) -> dict[str, Any]:
    """Read-only FIX status for dashboard/API — secrets redacted."""
    settings = settings or get_settings()
    store = FixQuoteStore.get()
    sym = settings.centroid_md_symbol_usdmxn or "USD/MXN"
    payload = store.diagnostics(primary_symbol=sym)
    payload["configured"] = settings.centroid_md_enabled
    payload["phase"] = "1_read_only_market_data"
    payload["trading_enabled"] = False
    payload["simulation_only"] = True
    if payload.get("session", {}).get("last_error"):
        payload["session"]["last_error"] = scrub(
            payload["session"]["last_error"],
            settings.centroid_md_password,
            settings.centroid_md_username,
        )
    for i, w in enumerate(payload.get("session", {}).get("warnings") or []):
        payload["session"]["warnings"][i] = scrub(
            w, settings.centroid_md_password, settings.centroid_md_username
        )
    payload["credentials"] = {
        "md_host_set": bool(settings.centroid_md_host),
        "md_username_set": bool(settings.centroid_md_username),
        "md_password_set": bool(settings.centroid_md_password),
        "md_sender_comp_id": settings.centroid_md_sender_comp_id,
        "md_target_comp_id": settings.centroid_md_target_comp_id,
    }
    return payload


def ensure_fix_session_started(settings: Settings | None = None) -> None:
    settings = settings or get_settings()
    if settings.centroid_md_enabled:
        start_centroid_md_background(settings)


def stop_fix_session(settings: Settings | None = None) -> None:
    settings = settings or get_settings()
    stop_centroid_md_background(settings)


__all__ = [
    "SimulatedOrder",
    "ensure_fix_session_started",
    "get_fix_diagnostics",
    "get_fix_quote",
    "stop_fix_session",
]
