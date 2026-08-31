"""Request helpers for customer sessions and admin-or-session access."""

from __future__ import annotations

from fastapi import HTTPException, Request

from app.config import get_settings
from app.services.admin_auth import _configured_secret, _provided_secret
from app.services.customer_session import COOKIE_NAME, verify_session_token


PUBLIC_PATH_PREFIXES = (
    "/health",
    "/auth/",
    "/docs",
    "/openapi.json",
    "/redoc",
)


def extract_session_token(request: Request) -> str | None:
    auth = request.headers.get("authorization") or ""
    if auth.lower().startswith("bearer "):
        token = auth[7:].strip()
        if token:
            return token
    cookie = request.cookies.get(COOKIE_NAME)
    return cookie or None


def get_customer_session(request: Request) -> dict | None:
    settings = get_settings()
    secret = settings.session_signing_secret
    token = extract_session_token(request)
    if not secret or not token:
        return None
    return verify_session_token(token, secret=secret)


def has_admin_secret(request: Request) -> bool:
    expected = _configured_secret()
    provided = _provided_secret(request)
    if not expected or not provided:
        return False
    import hmac

    return hmac.compare_digest(provided, expected)


def is_public_path(path: str) -> bool:
    if path == "/" or path == "/health":
        return True
    return any(path == prefix.rstrip("/") or path.startswith(prefix) for prefix in PUBLIC_PATH_PREFIXES)


def require_customer_session(request: Request) -> dict:
    session = get_customer_session(request)
    if session:
        return session
    raise HTTPException(status_code=401, detail="Authentication required")


def allow_customer_or_admin(request: Request) -> dict | None:
    session = get_customer_session(request)
    if session:
        return session
    if has_admin_secret(request):
        return {"role": "admin", "admin": True}
    settings = get_settings()
    if not settings.customer_auth_required:
        return None
    raise HTTPException(status_code=401, detail="Authentication required")
