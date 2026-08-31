"""HMAC token helpers compatible with Border Currency ``lib/auth-token.js``."""

from __future__ import annotations

import hashlib
import hmac
import json
import math
from typing import Any


def canonicalize(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, str):
        return json.dumps(value, ensure_ascii=False)
    if isinstance(value, int) and not isinstance(value, bool):
        return json.dumps(value)
    if isinstance(value, float):
        return "null" if not math.isfinite(value) else json.dumps(value)
    if isinstance(value, list):
        return "[" + ",".join(canonicalize(item) for item in value) + "]"
    if isinstance(value, dict):
        parts = []
        for key in sorted(value.keys()):
            if value[key] is None and key not in value:
                continue
            parts.append(json.dumps(str(key), ensure_ascii=False) + ":" + canonicalize(value[key]))
        return "{" + ",".join(parts) + "}"
    return "null"


def hmac_hex(secret: str, message: str) -> str:
    return hmac.new(
        secret.encode("utf-8"),
        message.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()


def timing_safe_equal(expected: str, actual: str) -> bool:
    left = expected.encode("utf-8")
    right = actual.encode("utf-8")
    if len(left) != len(right):
        return False
    return hmac.compare_digest(left, right)
