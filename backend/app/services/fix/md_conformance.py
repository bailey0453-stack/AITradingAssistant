"""Human-triggered Centroid FIX market-data conformance helpers.

These helpers are demo/read-only. They never submit trading orders.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Any

from app.config import Settings
from app.services.fix.centroid_md_session import get_centroid_md_session
from app.services.fix.messages import (
    build_market_data_request,
    build_market_data_unsubscribe,
    build_test_request,
)

logger = logging.getLogger(__name__)

_CONFORMANCE_LOCK = threading.Lock()
_SUSPEND_SETTLE_SECONDS = 0.50
_RESTORE_DELAY_SECONDS = 2.00


def _require_ready(settings: Settings):
    session = get_centroid_md_session(settings)
    if not session._sock or not session.store.health().fix_logged_on:  # noqa: SLF001
        raise ConnectionError("FIX market-data session is not logged on")
    return session


def _send_unsubscribe(session, settings: Settings, *, symbol: str, md_req_id: str) -> None:
    session._send(  # noqa: SLF001
        build_market_data_unsubscribe(
            seq_num=session._next_out_seq(),  # noqa: SLF001
            sender_comp_id=settings.centroid_md_sender_comp_id or "",
            target_comp_id=settings.centroid_md_target_comp_id or "",
            symbol=symbol,
            md_req_id=md_req_id,
        )
    )


def _send_subscription(
    session,
    settings: Settings,
    *,
    symbol: str,
    md_req_id: str,
    market_depth: str,
    record: bool = True,
) -> None:
    include_265 = bool(settings.centroid_md_include_md_update_type)
    msg = build_market_data_request(
        seq_num=session._next_out_seq(),  # noqa: SLF001
        sender_comp_id=settings.centroid_md_sender_comp_id or "",
        target_comp_id=settings.centroid_md_target_comp_id or "",
        symbol=symbol,
        md_req_id=md_req_id,
        subscription_type="1",
        market_depth=market_depth,
        include_md_update_type=include_265,
    )
    if record:
        session.store.record_md_request(
            md_req_id=md_req_id,
            symbol=symbol,
            subscription_request_type="1",
            market_depth=market_depth,
            md_update_type="1" if include_265 else None,
            entry_types=["0", "1"],
        )
    session._send(msg)  # noqa: SLF001


def _suspend_live_subscription(session, settings: Settings, symbol: str) -> str | None:
    """Temporarily remove the worker's normal live subscription, if present."""
    active_req_id = session._md_req_id  # noqa: SLF001
    if not active_req_id:
        return None
    _send_unsubscribe(session, settings, symbol=symbol, md_req_id=active_req_id)
    session._md_req_id = None  # noqa: SLF001
    time.sleep(_SUSPEND_SETTLE_SECONDS)
    return active_req_id


def _restore_live_subscription(
    session,
    settings: Settings,
    *,
    symbol: str,
    conformance_req_id: str,
    case: str,
) -> None:
    """Clean up the conformance subscription and restore the normal price feed."""
    try:
        time.sleep(_RESTORE_DELAY_SECONDS)
        if not session._sock or not session.store.health().fix_logged_on:  # noqa: SLF001
            logger.warning("Skipping FIX MD conformance restore because the session is no longer logged on")
            return

        history = session.store.diagnostics(primary_symbol=symbol).get("inbound_history") or []
        matched = [x for x in history if x.get("md_req_id") == conformance_req_id]
        conformance_was_active = any(x.get("msg_type") in {"W", "X"} for x in matched)

        # If the conformance subscription became active, remove it before
        # restoring the worker's normal long-running subscription. The
        # unsubscribe test already sent its own 263=2 message.
        if conformance_was_active and case != "unsubscribe":
            _send_unsubscribe(
                session,
                settings,
                symbol=symbol,
                md_req_id=conformance_req_id,
            )
            time.sleep(0.25)

        restore_req_id = f"MD-{int(time.time() * 1000)}"
        restore_depth = str(settings.centroid_md_market_depth)
        session._md_req_id = restore_req_id  # noqa: SLF001
        _send_subscription(
            session,
            settings,
            symbol=symbol,
            md_req_id=restore_req_id,
            market_depth=restore_depth,
        )
        logger.info(
            "Restored normal FIX MD subscription after conformance case %s with MDReqID %s",
            case,
            restore_req_id,
        )
    except Exception as exc:  # noqa: BLE001
        logger.exception("Failed to restore normal FIX MD subscription after conformance test: %s", exc)
        session.store.set_health(last_error=f"Conformance restore failed: {exc}")
    finally:
        if _CONFORMANCE_LOCK.locked():
            _CONFORMANCE_LOCK.release()


def _schedule_restore(
    session,
    settings: Settings,
    *,
    symbol: str,
    conformance_req_id: str,
    case: str,
) -> None:
    threading.Thread(
        target=_restore_live_subscription,
        kwargs={
            "session": session,
            "settings": settings,
            "symbol": symbol,
            "conformance_req_id": conformance_req_id,
            "case": case,
        },
        name=f"centroid-md-conformance-restore-{case}",
        daemon=True,
    ).start()


def send_test_request(settings: Settings) -> dict[str, Any]:
    session = _require_ready(settings)
    test_req_id = f"TEST-{int(time.time() * 1000)}"
    msg = build_test_request(
        seq_num=session._next_out_seq(),  # noqa: SLF001
        sender_comp_id=settings.centroid_md_sender_comp_id or "",
        target_comp_id=settings.centroid_md_target_comp_id or "",
        test_req_id=test_req_id,
    )
    session._send(msg)  # noqa: SLF001
    return {"sent": True, "case": "test_request", "test_req_id": test_req_id}


def send_md_case(settings: Settings, case: str) -> dict[str, Any]:
    if not _CONFORMANCE_LOCK.acquire(blocking=False):
        raise RuntimeError("Another market-data conformance case is still restoring the live subscription")

    try:
        session = _require_ready(settings)
        live_symbol = settings.centroid_md_symbol_usdmxn or "USDMXN.r"
        symbol = live_symbol
        req_id = f"CONFMD-{int(time.time() * 1000)}"
        depth = "1"

        suspended_req_id = _suspend_live_subscription(session, settings, live_symbol)

        if case == "subscribe_multiple":
            depth = "5"
        elif case == "wrong_symbol":
            symbol = "CONFORMANCE.INVALID"
        elif case not in {"subscribe_one", "unsubscribe", "duplicate_id"}:
            raise ValueError(f"Unknown MD conformance case: {case}")

        if case == "unsubscribe":
            _send_subscription(
                session,
                settings,
                symbol=symbol,
                md_req_id=req_id,
                market_depth="1",
            )
            time.sleep(0.25)
            _send_unsubscribe(session, settings, symbol=symbol, md_req_id=req_id)
            result = {
                "sent": True,
                "case": case,
                "md_req_id": req_id,
                "symbol": symbol,
                "market_depth": "1",
                "subscription_request_type": "2",
                "messages_sent": 2,
            }
        elif case == "duplicate_id":
            _send_subscription(
                session,
                settings,
                symbol=symbol,
                md_req_id=req_id,
                market_depth="1",
            )
            time.sleep(0.25)
            duplicate = build_market_data_request(
                seq_num=session._next_out_seq(),  # noqa: SLF001
                sender_comp_id=settings.centroid_md_sender_comp_id or "",
                target_comp_id=settings.centroid_md_target_comp_id or "",
                symbol=symbol,
                md_req_id=req_id,
                subscription_type="1",
                market_depth="1",
                include_md_update_type=bool(settings.centroid_md_include_md_update_type),
            )
            session._send(duplicate)  # noqa: SLF001
            result = {
                "sent": True,
                "case": case,
                "md_req_id": req_id,
                "symbol": symbol,
                "market_depth": "1",
                "subscription_request_type": "1",
                "messages_sent": 2,
            }
        else:
            _send_subscription(
                session,
                settings,
                symbol=symbol,
                md_req_id=req_id,
                market_depth=depth,
            )
            result = {
                "sent": True,
                "case": case,
                "md_req_id": req_id,
                "symbol": symbol,
                "market_depth": depth,
                "subscription_request_type": "1",
            }

        _schedule_restore(
            session,
            settings,
            symbol=live_symbol,
            conformance_req_id=req_id,
            case=case,
        )
        result.update(
            {
                "isolated_from_live_subscription": True,
                "suspended_md_req_id": suspended_req_id,
                "restore_scheduled": True,
            }
        )
        return result
    except Exception:
        if _CONFORMANCE_LOCK.locked():
            _CONFORMANCE_LOCK.release()
        raise
