"""In-memory FIX quote cache and session health (Phase 1)."""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Literal

MdSubscriptionStatus = Literal["none", "pending", "accepted", "rejected"]

FIX_MSG_TYPE_LABELS: dict[str, str] = {
    "0": "Heartbeat",
    "1": "TestRequest",
    "3": "Reject",
    "5": "Logout",
    "A": "Logon",
    "W": "MarketDataSnapshotFullRefresh",
    "X": "MarketDataIncrementalRefresh",
    "Y": "MarketDataRequestReject",
    "x": "SecurityListRequest",
    "y": "SecurityList",
    "j": "BusinessMessageReject",
}


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


@dataclass
class FixQuote:
    symbol: str
    bid: float | None = None
    ask: float | None = None
    spread: float | None = None
    updated_at: datetime = field(default_factory=_utcnow)
    source: str = "centroid_fix"

    def to_dict(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol,
            "bid": self.bid,
            "ask": self.ask,
            "spread": self.spread,
            "updated_at": self.updated_at.isoformat(),
            "source": self.source,
        }


@dataclass
class FixLastMdRequest:
    md_req_id: str | None = None
    symbol: str | None = None
    subscription_request_type: str | None = None
    market_depth: str | None = None
    md_update_type: str | None = None
    entry_types: list[str] = field(default_factory=list)
    sent_at: datetime | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "md_req_id": self.md_req_id,
            "symbol": self.symbol,
            "subscription_request_type": self.subscription_request_type,
            "market_depth": self.market_depth,
            "md_update_type": self.md_update_type,
            "entry_types": list(self.entry_types),
            "sent_at": self.sent_at.isoformat() if self.sent_at else None,
        }


@dataclass
class FixLastInbound:
    msg_type: str | None = None
    msg_type_label: str | None = None
    text: str | None = None
    business_reject_reason: str | None = None
    session_reject_reason: str | None = None
    md_req_reject_reason: str | None = None
    raw_reject_text: str | None = None
    received_at: datetime | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "msg_type": self.msg_type,
            "msg_type_label": self.msg_type_label,
            "text": self.text,
            "business_reject_reason": self.business_reject_reason,
            "session_reject_reason": self.session_reject_reason,
            "md_req_reject_reason": self.md_req_reject_reason,
            "raw_reject_text": self.raw_reject_text,
            "received_at": self.received_at.isoformat() if self.received_at else None,
        }


@dataclass
class FixSessionHealth:
    status: str = "disconnected"
    host: str | None = None
    port: int | None = None
    sender_comp_id: str | None = None
    target_comp_id: str | None = None
    ssl_enabled: bool = False
    tcp_connected: bool = False
    fix_logged_on: bool = False
    last_logon_at: datetime | None = None
    last_heartbeat_at: datetime | None = None
    last_quote_at: datetime | None = None
    last_error: str | None = None
    outbound_seq: int = 1
    inbound_seq: int = 1
    subscribed_symbol: str | None = None
    md_req_id: str | None = None
    md_subscription_status: MdSubscriptionStatus = "none"
    quotes_received_count: int = 0
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "host": self.host,
            "port": self.port,
            "sender_comp_id": self.sender_comp_id,
            "target_comp_id": self.target_comp_id,
            "ssl_enabled": self.ssl_enabled,
            "tcp_connected": self.tcp_connected,
            "fix_logged_on": self.fix_logged_on,
            "last_logon_at": self.last_logon_at.isoformat() if self.last_logon_at else None,
            "last_heartbeat_at": self.last_heartbeat_at.isoformat() if self.last_heartbeat_at else None,
            "last_quote_at": self.last_quote_at.isoformat() if self.last_quote_at else None,
            "last_error": self.last_error,
            "outbound_seq": self.outbound_seq,
            "inbound_seq": self.inbound_seq,
            "subscribed_symbol": self.subscribed_symbol,
            "md_req_id": self.md_req_id,
            "md_subscription_status": self.md_subscription_status,
            "quotes_received_count": self.quotes_received_count,
            "warnings": list(self.warnings),
        }


class FixQuoteStore:
    """Thread-safe singleton for latest FIX quotes, diagnostics, and symbol discovery."""

    _instance: "FixQuoteStore | None" = None
    _lock = threading.Lock()

    def __init__(self) -> None:
        self._quotes: dict[str, FixQuote] = {}
        self._health = FixSessionHealth()
        self._last_md_request = FixLastMdRequest()
        self._last_inbound = FixLastInbound()
        self._security_discovery: dict[str, Any] = {
            "status": "not_requested",
            "security_req_id": None,
            "request_result": None,
            "symbols": [],
            "usdmxn_candidates": [],
            "requested_at": None,
            "received_at": None,
            "error": None,
        }
        self._data_lock = threading.Lock()

    @classmethod
    def get(cls) -> "FixQuoteStore":
        with cls._lock:
            if cls._instance is None:
                cls._instance = cls()
            return cls._instance

    @classmethod
    def reset(cls) -> None:
        with cls._lock:
            cls._instance = None

    def update_quote(self, symbol: str, *, bid: float | None = None, ask: float | None = None) -> FixQuote:
        with self._data_lock:
            existing = self._quotes.get(symbol)
            new_bid = bid if bid is not None else (existing.bid if existing else None)
            new_ask = ask if ask is not None else (existing.ask if existing else None)
            spread = round(new_ask - new_bid, 6) if new_bid is not None and new_ask is not None else None
            quote = FixQuote(symbol=symbol, bid=new_bid, ask=new_ask, spread=spread, updated_at=_utcnow())
            self._quotes[symbol] = quote
            self._health.last_quote_at = quote.updated_at
            self._health.quotes_received_count += 1
            self._health.md_subscription_status = "accepted"
            return quote

    def get_quote(self, symbol: str) -> FixQuote | None:
        with self._data_lock:
            return self._quotes.get(symbol)

    def health(self) -> FixSessionHealth:
        with self._data_lock:
            return self._health

    def set_health(self, **kwargs) -> None:
        with self._data_lock:
            for key, val in kwargs.items():
                if hasattr(self._health, key):
                    setattr(self._health, key, val)

    def record_md_request(self, *, md_req_id: str, symbol: str, subscription_request_type: str, market_depth: str, md_update_type: str | None, entry_types: list[str]) -> None:
        with self._data_lock:
            self._last_md_request = FixLastMdRequest(
                md_req_id=md_req_id,
                symbol=symbol,
                subscription_request_type=subscription_request_type,
                market_depth=market_depth,
                md_update_type=md_update_type,
                entry_types=list(entry_types),
                sent_at=_utcnow(),
            )
            self._health.md_req_id = md_req_id
            self._health.subscribed_symbol = symbol
            self._health.md_subscription_status = "pending"

    def record_security_request(self, security_req_id: str) -> None:
        with self._data_lock:
            self._security_discovery = {
                "status": "pending",
                "security_req_id": security_req_id,
                "request_result": None,
                "symbols": [],
                "usdmxn_candidates": [],
                "requested_at": _utcnow().isoformat(),
                "received_at": None,
                "error": None,
            }

    def record_security_list(self, *, security_req_id: str | None, request_result: str | None, symbols: list[str]) -> None:
        clean = sorted({s.strip() for s in symbols if s and s.strip()})
        candidates = [s for s in clean if "USD" in s.upper() and "MXN" in s.upper()]
        with self._data_lock:
            requested_at = self._security_discovery.get("requested_at")
            self._security_discovery = {
                "status": "received",
                "security_req_id": security_req_id or self._security_discovery.get("security_req_id"),
                "request_result": request_result,
                "symbols": clean,
                "usdmxn_candidates": candidates,
                "requested_at": requested_at,
                "received_at": _utcnow().isoformat(),
                "error": None,
            }

    def record_security_discovery_error(self, error: str) -> None:
        with self._data_lock:
            self._security_discovery["status"] = "rejected"
            self._security_discovery["error"] = error
            self._security_discovery["received_at"] = _utcnow().isoformat()

    def security_discovery(self) -> dict[str, Any]:
        with self._data_lock:
            return {
                **self._security_discovery,
                "symbols": list(self._security_discovery.get("symbols") or []),
                "usdmxn_candidates": list(self._security_discovery.get("usdmxn_candidates") or []),
            }

    def record_inbound(self, *, msg_type: str, fmap: dict[str, str], raw_summary: str | None = None) -> None:
        label = FIX_MSG_TYPE_LABELS.get(msg_type, msg_type)
        text = fmap.get("58")
        inbound = FixLastInbound(
            msg_type=msg_type,
            msg_type_label=label,
            text=text,
            business_reject_reason=fmap.get("380") if msg_type == "j" else None,
            session_reject_reason=fmap.get("373") if msg_type == "3" else None,
            md_req_reject_reason=text if msg_type == "Y" else None,
            raw_reject_text=raw_summary or text,
            received_at=_utcnow(),
        )
        with self._data_lock:
            self._last_inbound = inbound

    def last_md_request(self) -> FixLastMdRequest:
        with self._data_lock:
            return self._last_md_request

    def last_inbound(self) -> FixLastInbound:
        with self._data_lock:
            return self._last_inbound

    def diagnostics(self, *, primary_symbol: str | None = None) -> dict[str, Any]:
        with self._data_lock:
            health = self._health.to_dict()
            quote = None
            if primary_symbol and primary_symbol in self._quotes:
                quote = self._quotes[primary_symbol].to_dict()
            elif self._quotes:
                quote = next(iter(self._quotes.values())).to_dict()
            discovery = {
                **self._security_discovery,
                "symbols": list(self._security_discovery.get("symbols") or []),
                "usdmxn_candidates": list(self._security_discovery.get("usdmxn_candidates") or []),
            }
            return {
                "session": health,
                "quote": quote,
                "quote_count": len(self._quotes),
                "last_md_request": self._last_md_request.to_dict(),
                "last_inbound": self._last_inbound.to_dict(),
                "security_discovery": discovery,
            }
