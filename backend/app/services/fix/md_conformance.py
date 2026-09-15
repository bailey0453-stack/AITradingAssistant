"""Human-triggered Centroid FIX market-data conformance helpers.

These helpers are demo/read-only. They never submit trading orders.
"""

from __future__ import annotations

import time
from typing import Any

from app.config import Settings
from app.services.fix.centroid_md_session import get_centroid_md_session
from app.services.fix.messages import (
    build_market_data_request,
    build_market_data_unsubscribe,
    build_test_request,
)


def _require_ready(settings: Settings):
    session = get_centroid_md_session(settings)
    if not session._sock or not session.store.health().fix_logged_on:  # noqa: SLF001
        raise ConnectionError("FIX market-data session is not logged on")
    return session


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
    session = _require_ready(settings)
    symbol = settings.centroid_md_symbol_usdmxn or "USDMXN.r"
    req_id = f"CONFMD-{int(time.time() * 1000)}"
    depth = "1"

    if case == "subscribe_one":
        pass
    elif case == "subscribe_multiple":
        depth = "5"
    elif case == "wrong_symbol":
        symbol = "CONFORMANCE.INVALID"
    elif case == "unsubscribe":
        first = build_market_data_request(
            seq_num=session._next_out_seq(),  # noqa: SLF001
            sender_comp_id=settings.centroid_md_sender_comp_id or "",
            target_comp_id=settings.centroid_md_target_comp_id or "",
            symbol=symbol,
            md_req_id=req_id,
            subscription_type="1",
            market_depth="1",
            include_md_update_type=True,
        )
        session.store.record_md_request(
            md_req_id=req_id,
            symbol=symbol,
            subscription_request_type="1",
            market_depth="1",
            md_update_type="1",
            entry_types=["0", "1"],
        )
        session._send(first)  # noqa: SLF001
        time.sleep(0.25)
        session._send(  # noqa: SLF001
            build_market_data_unsubscribe(
                seq_num=session._next_out_seq(),  # noqa: SLF001
                sender_comp_id=settings.centroid_md_sender_comp_id or "",
                target_comp_id=settings.centroid_md_target_comp_id or "",
                symbol=symbol,
                md_req_id=req_id,
            )
        )
        return {
            "sent": True,
            "case": case,
            "md_req_id": req_id,
            "symbol": symbol,
            "market_depth": "1",
            "subscription_request_type": "2",
            "messages_sent": 2,
        }
    elif case == "duplicate_id":
        msg = build_market_data_request(
            seq_num=session._next_out_seq(),  # noqa: SLF001
            sender_comp_id=settings.centroid_md_sender_comp_id or "",
            target_comp_id=settings.centroid_md_target_comp_id or "",
            symbol=symbol,
            md_req_id=req_id,
            subscription_type="1",
            market_depth="1",
            include_md_update_type=True,
        )
        session.store.record_md_request(
            md_req_id=req_id,
            symbol=symbol,
            subscription_request_type="1",
            market_depth="1",
            md_update_type="1",
            entry_types=["0", "1"],
        )
        session._send(msg)  # noqa: SLF001
        time.sleep(0.25)
        duplicate = build_market_data_request(
            seq_num=session._next_out_seq(),  # noqa: SLF001
            sender_comp_id=settings.centroid_md_sender_comp_id or "",
            target_comp_id=settings.centroid_md_target_comp_id or "",
            symbol=symbol,
            md_req_id=req_id,
            subscription_type="1",
            market_depth="1",
            include_md_update_type=True,
        )
        session._send(duplicate)  # noqa: SLF001
        return {
            "sent": True,
            "case": case,
            "md_req_id": req_id,
            "symbol": symbol,
            "market_depth": "1",
            "subscription_request_type": "1",
            "messages_sent": 2,
        }
    else:
        raise ValueError(f"Unknown MD conformance case: {case}")

    msg = build_market_data_request(
        seq_num=session._next_out_seq(),  # noqa: SLF001
        sender_comp_id=settings.centroid_md_sender_comp_id or "",
        target_comp_id=settings.centroid_md_target_comp_id or "",
        symbol=symbol,
        md_req_id=req_id,
        subscription_type="1",
        market_depth=depth,
        include_md_update_type=True,
    )
    session.store.record_md_request(
        md_req_id=req_id,
        symbol=symbol,
        subscription_request_type="1",
        market_depth=depth,
        md_update_type="1",
        entry_types=["0", "1"],
    )
    session._send(msg)  # noqa: SLF001
    return {
        "sent": True,
        "case": case,
        "md_req_id": req_id,
        "symbol": symbol,
        "market_depth": depth,
        "subscription_request_type": "1",
    }
