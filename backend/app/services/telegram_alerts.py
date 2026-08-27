"""Telegram notifications for actionable FX signal transitions."""

from __future__ import annotations

import logging
import threading

import httpx
from sqlalchemy import event, select

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
        response = httpx.post(
            f"https://api.telegram.org/bot{settings.telegram_bot_token}/sendMessage",
            json={"chat_id": settings.telegram_chat_id, "text": text},
            timeout=min(float(settings.http_timeout_seconds or 8), 8.0),
        )
        response.raise_for_status()
        ok = bool(response.json().get("ok"))
        logger.info("Telegram delivery %s", "succeeded" if ok else "returned ok=false")
        return ok
    except Exception:  # noqa: BLE001
        logger.exception("Telegram delivery failed")
        return False


def _message(reco: Recommendation, previous: str | None) -> str:
    direction = str(reco.direction or "")
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
    return "\n".join(lines)


@event.listens_for(Recommendation, "after_insert")
def _alert_after_insert(mapper, connection, target: Recommendation) -> None:  # noqa: ARG001
    """Send only when the persisted direction transitions into BUY/SELL.

    The prior direction is read from durable recommendation history, so cold
    starts and multiple Vercel instances do not reset duplicate suppression.
    Delivery runs off-thread and cannot block/abort the recommendation commit.
    """
    direction = str(target.direction or "")
    if direction not in _ACTIONABLE:
        return
    previous = connection.execute(
        select(Recommendation.direction)
        .where(Recommendation.id < target.id)
        .order_by(Recommendation.id.desc())
        .limit(1)
    ).scalar_one_or_none()
    if previous == direction:
        return
    text = _message(target, previous)
    threading.Thread(target=send_message, args=(text,), daemon=True).start()


def send_test_alert() -> bool:
    return send_message("FX Intelligence Telegram test\nConnection successful. BUY/SELL alerts are enabled.")
