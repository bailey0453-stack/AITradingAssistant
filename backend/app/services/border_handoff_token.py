"""Verify short-lived Border Currency AI Trading Assistant handoff tokens."""

from __future__ import annotations

import base64
import json
from typing import Any

from app.services.border_hmac import canonicalize, hmac_hex, timing_safe_equal

AUDIENCE = "ai-trading-assistant"
ISSUER = "border-currency-shipments"


class HandoffTokenError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


def _decode_payload(token: str) -> dict[str, Any] | None:
    raw = (token or "").strip()
    if not raw:
        return None
    try:
        padded = raw + "=" * (-len(raw) % 4)
        parsed = json.loads(base64.b64decode(padded).decode("utf-8"))
    except (ValueError, json.JSONDecodeError, UnicodeDecodeError):
        return None
    if not isinstance(parsed, dict):
        return None
    return parsed


def encode_handoff_token(
    claims: dict[str, Any],
    *,
    secret: str,
    now_sec: int,
    ttl_sec: int = 300,
    audience: str = AUDIENCE,
    issuer: str = ISSUER,
) -> str:
    payload = {
        "userId": claims.get("userId"),
        "email": claims.get("email"),
        "name": claims.get("name") or "",
        "role": claims.get("role") or "",
        "iat": int(now_sec),
        "exp": int(now_sec) + int(ttl_sec),
        "ver": 1,
        "aud": audience,
        "iss": issuer,
    }
    sig = hmac_hex(secret, canonicalize(payload))
    raw = json.dumps({**payload, "sig": sig}, separators=(",", ":")).encode("utf-8")
    return base64.b64encode(raw).decode("ascii")


def verify_handoff_token(
    token: str,
    *,
    secret: str,
    now_sec: int,
    audience: str = AUDIENCE,
    issuer: str = ISSUER,
) -> dict[str, Any]:
    if not secret:
        raise HandoffTokenError("sso_secret_missing", "SSO signing secret is not configured")

    parsed = _decode_payload(token)
    if not parsed:
        raise HandoffTokenError("invalid_token", "Invalid access token")

    sig = parsed.get("sig")
    if not isinstance(sig, str) or not sig:
        raise HandoffTokenError("invalid_token", "Invalid access token")

    try:
        iat = int(parsed["iat"])
        exp = int(parsed["exp"])
        ver = int(parsed["ver"])
    except (KeyError, TypeError, ValueError):
        raise HandoffTokenError("invalid_token", "Invalid access token") from None
    if ver < 1:
        raise HandoffTokenError("invalid_token", "Invalid access token")
    if parsed.get("aud") != audience or parsed.get("iss") != issuer:
        raise HandoffTokenError("invalid_audience", "Invalid access token audience")
    if now_sec >= exp:
        raise HandoffTokenError("token_expired", "Access token has expired")
    if iat - 30 > now_sec:
        raise HandoffTokenError("invalid_token", "Access token is not yet valid")

    claims = dict(parsed)
    claims.pop("sig", None)
    expected = hmac_hex(secret, canonicalize(claims))
    if not timing_safe_equal(expected, sig):
        raise HandoffTokenError("invalid_signature", "Invalid access token")

    return claims
