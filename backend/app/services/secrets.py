"""Helpers for keeping API keys out of logs.

Every live provider sends its key via a request header (never the URL) and runs
any outbound error text through :func:`scrub` before logging, so a stray
exception string can never leak a secret.
"""

from __future__ import annotations

from typing import Iterable, Optional


def scrub(text: str, *secrets: Optional[str]) -> str:
    """Redact any of ``secrets`` from ``text`` before it is logged."""
    cleaned = text
    for secret in secrets:
        if secret and secret in cleaned:
            cleaned = cleaned.replace(secret, "***REDACTED***")
    return cleaned


def scrub_all(text: str, secrets: Iterable[Optional[str]]) -> str:
    return scrub(text, *list(secrets))
