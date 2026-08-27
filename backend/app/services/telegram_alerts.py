"""Telegram notifications for actionable FX signal transitions."""

from __future__ import annotations

import logging
from typing import Optional

import requests
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.models import Recommendation

logger = logging.getLogger(__name__)
_ACTIONABLE = {"BUY_USD", "SELL_USD"}


def _fmt(value: object, digits: int = 4) -> str:
    try:
        return f"{float(value):.{digits}f}"
    except (TypeError, ValueError):
        return "n/a"


def send_message(text: str) -> bool:
    """Best-effort Telegram delivery. Never raise into analysis."""
    settings = get_settings()
    if not settings.telegram_configured:
        logger.info("Telegram alerts not configured; skipping message")
        return False
    try:
        response = requests.post(
            f"https://api.telegram.org/bot{settings.telegram_bot_token}/sendMessage",
            json={"chat_id": settings.telegram_chat_id, "text": text},
            timeout=min(float(settings.http_timeout_seconds or 8), 8.0),
        )
        response.raise_for_status()
        payload = response.json()
        if not payload.get("ok"):
            logger.warning("Telegram returned ok=false")
            return False
        return True
    except Exception:  # noqa: BLE001
        logger.exception("Telegram delivery failed")
        return False


def _previous_direction(db: Session, current_id: int) -> Optional[str]:
    """Direction immediately before the newly stored recommendation."""
    return db.execute(
        select(Recommendation.direction)
        .where(Recommendation.id < current_id)
        .order_by(Recommendation.id.desc())
        .limit(1)
    ).scalar_one_or_none()


def maybe_send_signal_alert(db: Session, reco: Recommendation) -> bool:
    """Alert once when the persisted signal transitions into BUY or SELL.

    Repeated SELL->SELL or BUY->BUY analyses are silent. WAIT/NO_TRADE rows are
    intentionally persisted by the normal recommendation flow, so a later
    WAIT->BUY or WAIT->SELL transition produces a fresh alert.
    """
    direction = str(reco.direction or "")
    if direction not in _ACTIONABLE:
        return False
    previous = _previous_direction(db, reco.id)
    if previous == direction:
        return False

    label = "BUY USD / SELL MXN" if direction == "BUY_USD" else "SELL USD / BUY MXN"
    lines = [
        "FX Intelligence Signal",
        label,
        f"FIX bid / ask: {_fmt(reco.fix_bid, 5)} / {_fmt(reco.fix_ask, 5)}",
        f"Market: {_fmt(reco.spot_price, 4)}",
        f"Grade: {reco.opportunity_grade or 'n/a'} | Confidence: {_fmt(reco.confidence, 1)}%",
    ]
    if reco.target is not None:
        lines.append(f"Target: {_fmt(reco.target, 4)}")
    if reco.stop is not None:
        invalid = "Invalid above" if direction == "SELL_USD" else "Invalid below"
        lines.append(f"{invalid}: {_fmt(reco.stop, 4)}")
    if previous:
        lines.append(f"Signal changed: {previous} -> {direction}")
    return send_message("\n".join(lines))


def send_test_alert() -> bool:
    return send_message("FX Intelligence Telegram test\nConnection successful. BUY/SELL alerts are enabled.")
