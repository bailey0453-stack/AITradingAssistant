"""Signed AI Trading Assistant customer sessions (post-SSO)."""

from __future__ import annotations

import base64
import json
import time
from typing import Any

from app.services.border_hmac import canonicalize, hmac_hex, timing_safe_equal

SESSION_AUDIENCE = "ai-trading-assistant-session"
COOKIE_NAME = "aita_session"


def encode_session_token(
    claims: dict[str, Any],
    *,
    secret: str,
    ttl_sec: int,
    now_sec: int | None = None,
) -> str:
    issued = int(now_sec if now_sec is not None else time.time())
    payload = {
        "userId": claims.get("userId"),
        "email": claims.get("email"),
        "name": claims.get("name") or "",
        "role": claims.get("role") or "",
        "aud": SESSION_AUDIENCE,
        "iat": issued,
        "exp": issued + int(ttl_sec),
        "ver": 1,
    }
    sig = hmac_hex(secret, canonicalize(payload))
    return base64.b64encode(json.dumps({**payload, "sig": sig}, separators=(",", ":")).encode("utf-8")).decode("ascii")


def verify_session_token(
    token: str,
    *,
    secret: str,
    now_sec: int | None = None,
) -> dict[str, Any] | None:
    if not token or not secret:
        return None
    try:
        padded = token + "=" * (-len(token) % 4)
        parsed = json.loads(base64.b64decode(padded).decode("utf-8"))
    except (ValueError, json.JSONDecodeError, UnicodeDecodeError):
        return None
    if not isinstance(parsed, dict):
        return None
    sig = parsed.get("sig")
    if not isinstance(sig, str) or not sig:
        return None
    if parsed.get("aud") != SESSION_AUDIENCE:
        return None
    try:
        exp = int(parsed["exp"])
        int(parsed["iat"])
        int(parsed["ver"])
    except (KeyError, TypeError, ValueError):
        return None
    now = int(now_sec if now_sec is not None else time.time())
    if now >= exp:
        return None
    claims = dict(parsed)
    claims.pop("sig", None)
    expected = hmac_hex(secret, canonicalize(claims))
    if not timing_safe_equal(expected, sig):
        return None
    return claims
