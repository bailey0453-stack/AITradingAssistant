"""FIX 4.4 administrative and market-data message builders (Phase 1)."""

from __future__ import annotations

import uuid

from app.services.fix.codec import encode_message


def build_logon(*, seq_num: int, sender_comp_id: str, target_comp_id: str, username: str | None = None, password: str | None = None, heart_bt_int: int = 30, reset_seq_num: bool = False, sending_time: str | None = None) -> str:
    fields: list[tuple[str, str]] = [("98", "0"), ("108", str(heart_bt_int))]
    if reset_seq_num:
        fields.append(("141", "Y"))
    if username:
        fields.append(("553", username))
    if password:
        fields.append(("554", password))
    return encode_message("A", fields, seq_num=seq_num, sender_comp_id=sender_comp_id, target_comp_id=target_comp_id, sending_time=sending_time)


def build_heartbeat(*, seq_num: int, sender_comp_id: str, target_comp_id: str, test_req_id: str | None = None, sending_time: str | None = None) -> str:
    fields: list[tuple[str, str]] = []
    if test_req_id:
        fields.append(("112", test_req_id))
    return encode_message("0", fields, seq_num=seq_num, sender_comp_id=sender_comp_id, target_comp_id=target_comp_id, sending_time=sending_time)


def build_test_request(*, seq_num: int, sender_comp_id: str, target_comp_id: str, test_req_id: str, sending_time: str | None = None) -> str:
    return encode_message("1", [("112", test_req_id)], seq_num=seq_num, sender_comp_id=sender_comp_id, target_comp_id=target_comp_id, sending_time=sending_time)


def build_logout(*, seq_num: int, sender_comp_id: str, target_comp_id: str, text: str | None = None, sending_time: str | None = None) -> str:
    fields: list[tuple[str, str]] = []
    if text:
        fields.append(("58", text))
    return encode_message("5", fields, seq_num=seq_num, sender_comp_id=sender_comp_id, target_comp_id=target_comp_id, sending_time=sending_time)


def build_security_list_request(*, seq_num: int, sender_comp_id: str, target_comp_id: str, security_req_id: str | None = None, subscription_request_type: str = "0", sending_time: str | None = None) -> str:
    """SecurityListRequest (35=x) asking the venue for all securities available to this session."""
    req_id = security_req_id or f"SEC-{uuid.uuid4().hex[:12]}"
    return encode_message(
        "x",
        [("320", req_id), ("559", "4"), ("263", subscription_request_type)],
        seq_num=seq_num,
        sender_comp_id=sender_comp_id,
        target_comp_id=target_comp_id,
        sending_time=sending_time,
    )


def build_market_data_request(*, seq_num: int, sender_comp_id: str, target_comp_id: str, symbol: str, md_req_id: str | None = None, subscription_type: str = "1", market_depth: str = "1", md_update_type: str = "1", include_md_update_type: bool = True, sending_time: str | None = None) -> str:
    """MarketDataRequest (35=V) — subscribe to top-of-book bid/offer."""
    req_id = md_req_id or f"MD-{uuid.uuid4().hex[:12]}"
    fields: list[tuple[str, str]] = [("262", req_id), ("263", subscription_type), ("264", market_depth)]
    if include_md_update_type:
        fields.append(("265", md_update_type))
    fields.extend([("267", "2"), ("269", "0"), ("269", "1"), ("146", "1"), ("55", symbol)])
    return encode_message("V", fields, seq_num=seq_num, sender_comp_id=sender_comp_id, target_comp_id=target_comp_id, sending_time=sending_time)


def build_market_data_unsubscribe(*, seq_num: int, sender_comp_id: str, target_comp_id: str, symbol: str, md_req_id: str, sending_time: str | None = None) -> str:
    return build_market_data_request(seq_num=seq_num, sender_comp_id=sender_comp_id, target_comp_id=target_comp_id, symbol=symbol, md_req_id=md_req_id, subscription_type="2", sending_time=sending_time)
