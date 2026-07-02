"""FIX 4.4 message encoding and checksum (no external QuickFIX dependency)."""

from __future__ import annotations

from datetime import datetime, timezone

SOH = "\x01"
FIX_VERSION = "FIX.4.4"


def utc_sending_time(dt: datetime | None = None) -> str:
    """FIX UTC timestamp (Tag 52), millisecond precision."""
    dt = dt or datetime.now(timezone.utc)
    return dt.strftime("%Y%m%d-%H:%M:%S.%f")[:-3]


def checksum_value(payload: str) -> int:
    """Tag 10 — sum of all bytes before the checksum field, mod 256."""
    return sum(payload.encode("ascii")) % 256


def body_length(body: str) -> int:
    return len(body.encode("ascii"))


def encode_message(
    msg_type: str,
    fields: list[tuple[str, str]],
    *,
    seq_num: int,
    sender_comp_id: str,
    target_comp_id: str,
    sending_time: str | None = None,
) -> str:
    """Build a complete FIX 4.4 message string (including checksum)."""
    ts = sending_time or utc_sending_time()
    body_parts = [
        f"35={msg_type}",
        f"49={sender_comp_id}",
        f"56={target_comp_id}",
        f"34={seq_num}",
        f"52={ts}",
    ]
    for tag, value in fields:
        body_parts.append(f"{tag}={value}")
    body = SOH.join(body_parts) + SOH
    header = f"8={FIX_VERSION}{SOH}9={body_length(body)}{SOH}"
    without_checksum = header + body
    csum = checksum_value(without_checksum)
    return without_checksum + f"10={csum:03d}{SOH}"


def split_messages(buffer: str) -> tuple[list[str], str]:
    """Split a FIX byte stream buffer into complete messages; return remainder."""
    messages: list[str] = []
    while True:
        start = buffer.find("8=FIX")
        if start == -1:
            return messages, buffer
        if start > 0:
            buffer = buffer[start:]
        csum_idx = buffer.find(f"{SOH}10=")
        if csum_idx == -1:
            return messages, buffer
        end = buffer.find(SOH, csum_idx + 1)
        if end == -1:
            return messages, buffer
        messages.append(buffer[: end + 1])
        buffer = buffer[end + 1 :]
        if not buffer:
            break
    return messages, buffer


def parse_fields(raw: str) -> list[tuple[str, str]]:
    """Parse FIX message into ordered (tag, value) pairs."""
    text = raw.rstrip(SOH)
    if not text:
        return []
    out: list[tuple[str, str]] = []
    for part in text.split(SOH):
        if "=" not in part:
            continue
        tag, val = part.split("=", 1)
        out.append((tag, val))
    return out


def field_map(raw: str) -> dict[str, str]:
    """First occurrence wins (non-repeating tags)."""
    out: dict[str, str] = {}
    for tag, val in parse_fields(raw):
        out.setdefault(tag, val)
    return out


def validate_message(raw: str) -> dict:
    """Validate body length and checksum; return diagnostics dict."""
    fields = parse_fields(raw)
    fmap = {t: v for t, v in fields}
    msg_type = fmap.get("35", "")
    declared_len = int(fmap.get("9", "0") or "0")
    declared_csum = fmap.get("10", "")

    csum_pos = raw.rfind(f"{SOH}10=")
    if csum_pos == -1:
        return {
            "valid": False,
            "msg_type": msg_type,
            "error": "missing checksum",
        }
    without_csum = raw[:csum_pos + 1]  # include SOH before 10=
    # Body length covers bytes after 9=NNN SOH through end of body (before 10=)
    nine_pos = raw.find(f"{SOH}9=")
    body_start = raw.find(SOH, nine_pos + 1) + 1 if nine_pos != -1 else 0
    body = raw[body_start:csum_pos + 1]
    actual_len = body_length(body)
    actual_csum = checksum_value(without_csum)

    return {
        "valid": actual_len == declared_len and declared_csum == f"{actual_csum:03d}",
        "msg_type": msg_type,
        "declared_body_length": declared_len,
        "actual_body_length": actual_len,
        "declared_checksum": declared_csum,
        "actual_checksum": f"{actual_csum:03d}",
    }
