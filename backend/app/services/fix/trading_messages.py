"""Centroid/GFC FIX 4.4 trading message builders for demo conformance."""

from __future__ import annotations

from datetime import datetime, timezone
import uuid

from app.services.fix.codec import encode_message


def _utc_timestamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%d-%H:%M:%S.%f")[:-3]


def new_cl_ord_id(prefix: str = "CONF") -> str:
    """Return a Centroid-safe ClOrdID using only documented characters.

    Centroid v0.16.9 documents A-Z, a-z, '.', '-', '_' for tag 11. UUID hex
    normally contains digits, so translate 0-9 to letters before truncating.
    """
    token = uuid.uuid4().hex.translate(str.maketrans("0123456789", "ghijklmnop"))[:12]
    return f"{prefix}-{token}"


def build_new_order_single(
    *,
    seq_num: int,
    sender_comp_id: str,
    target_comp_id: str,
    account: str,
    symbol: str,
    side: str,
    quantity: float,
    ord_type: str,
    time_in_force: str,
    cl_ord_id: str | None = None,
    price: float | None = None,
    ttl_ms: int | None = None,
    sending_time: str | None = None,
) -> str:
    """Build NewOrderSingle (35=D) per Centroid v0.16.9."""
    order_id = cl_ord_id or new_cl_ord_id()
    fields: list[tuple[str, str]] = [
        ("11", order_id),
        ("1", account),
        ("55", symbol),
        ("54", str(side)),
        ("38", f"{quantity:g}"),
        ("40", str(ord_type)),
        ("59", str(time_in_force)),
        ("60", sending_time or _utc_timestamp()),
    ]
    if str(ord_type) == "2":
        if price is None:
            raise ValueError("Limit orders require price")
        fields.append(("44", f"{price:g}"))
    if ttl_ms is not None:
        fields.append(("90000", str(int(ttl_ms))))
    return encode_message(
        "D",
        fields,
        seq_num=seq_num,
        sender_comp_id=sender_comp_id,
        target_comp_id=target_comp_id,
        sending_time=sending_time,
    )


def build_order_status_request(
    *,
    seq_num: int,
    sender_comp_id: str,
    target_comp_id: str,
    account: str,
    cl_ord_id: str,
    order_id: str | None = None,
    sending_time: str | None = None,
) -> str:
    fields = [("11", cl_ord_id), ("1", account)]
    if order_id:
        fields.insert(0, ("37", order_id))
    return encode_message(
        "H",
        fields,
        seq_num=seq_num,
        sender_comp_id=sender_comp_id,
        target_comp_id=target_comp_id,
        sending_time=sending_time,
    )


def build_order_cancel_request(
    *,
    seq_num: int,
    sender_comp_id: str,
    target_comp_id: str,
    account: str,
    orig_cl_ord_id: str,
    side: str,
    cl_ord_id: str | None = None,
    sending_time: str | None = None,
) -> str:
    fields = [
        ("11", cl_ord_id or new_cl_ord_id("CXL")),
        ("1", account),
        ("41", orig_cl_ord_id),
        ("54", str(side)),
        ("60", sending_time or _utc_timestamp()),
    ]
    return encode_message(
        "F",
        fields,
        seq_num=seq_num,
        sender_comp_id=sender_comp_id,
        target_comp_id=target_comp_id,
        sending_time=sending_time,
    )
