"""Admin endpoint authentication (cron secret or dedicated admin secret)."""

from __future__ import annotations

import hmac
import os

from fastapi import HTTPException, Request

from app.config import get_settings


def _configured_secret() -> str | None:
    settings = get_settings()
    return (
        os.getenv("ADMIN_SECRET")
        or os.getenv("CRON_SECRET")
        or settings.cron_secret
    )


def _provided_secret(request: Request) -> str | None:
    auth = request.headers.get("authorization") or ""
    if auth.lower().startswith("bearer "):
        return auth[7:].strip()
    return (
        request.headers.get("x-admin-secret")
        or request.headers.get("x-cron-secret")
        or request.query_params.get("secret")
    )


def require_admin_auth(request: Request) -> None:
    """Protect admin import endpoints."""
    expected = _configured_secret()
    provided = _provided_secret(request)

    if not expected:
        if get_settings().is_mock:
            return
        raise HTTPException(
            status_code=503,
            detail="ADMIN_SECRET or CRON_SECRET not configured",
        )

    if not provided or not hmac.compare_digest(provided, expected):
        raise HTTPException(status_code=401, detail="Invalid or missing admin secret")
