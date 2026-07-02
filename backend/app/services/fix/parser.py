"""Parse FIX market-data snapshot and incremental refresh messages."""

from __future__ import annotations

from app.services.fix.codec import parse_fields


def _md_entries(fields: list[tuple[str, str]]) -> list[dict[str, str]]:
    """Extract repeating MD entry groups (269/270/271) after tag 268."""
    entries: list[dict[str, str]] = []
    current: dict[str, str] = {}
    in_group = False
    count = 0
    seen = 0
    for tag, val in fields:
        if tag == "268":
            in_group = True
            try:
                count = int(val)
            except ValueError:
                count = 0
            continue
        if not in_group:
            continue
        if tag == "269":
            if current:
                entries.append(current)
                seen += 1
            current = {"269": val}
            continue
        if tag in {"270", "271", "272", "273"}:
            current[tag] = val
    if current:
        entries.append(current)
        seen += 1
    if count and seen != count:
        pass  # tolerate partial groups
    return entries


def parse_market_data_snapshot(raw: str) -> dict:
    """MarketDataSnapshotFullRefresh (35=W) -> symbol, bid, ask."""
    fields = parse_fields(raw)
    fmap = {t: v for t, v in fields}
    symbol = fmap.get("55", "")
    bid = ask = None
    for entry in _md_entries(fields):
        etype = entry.get("269")
        px = entry.get("270")
        if px is None:
            continue
        try:
            price = float(px)
        except ValueError:
            continue
        if etype == "0":
            bid = price
        elif etype == "1":
            ask = price
    return {"symbol": symbol, "bid": bid, "ask": ask, "msg_type": "W"}


def parse_market_data_incremental(raw: str) -> dict:
    """MarketDataIncrementalRefresh (35=X) — best-effort bid/ask update."""
    fields = parse_fields(raw)
    fmap = {t: v for t, v in fields}
    symbol = fmap.get("55", "")
    bid = ask = None
    for entry in _md_entries(fields):
        etype = entry.get("269")
        px = entry.get("270")
        if px is None:
            continue
        try:
            price = float(px)
        except ValueError:
            continue
        if etype == "0":
            bid = price
        elif etype == "1":
            ask = price
    return {"symbol": symbol, "bid": bid, "ask": ask, "msg_type": "X"}
