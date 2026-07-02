"""In-memory FIX quote cache and session health (Phase 1)."""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any


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
class FixSessionHealth:
    status: str = "disconnected"  # disconnected | connecting | connected | error
    host: str | None = None
    port: int | None = None
    sender_comp_id: str | None = None
    target_comp_id: str | None = None
    ssl_enabled: bool = False
    last_logon_at: datetime | None = None
    last_heartbeat_at: datetime | None = None
    last_quote_at: datetime | None = None
    last_error: str | None = None
    outbound_seq: int = 1
    inbound_seq: int = 1
    subscribed_symbol: str | None = None
    md_req_id: str | None = None
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "host": self.host,
            "port": self.port,
            "sender_comp_id": self.sender_comp_id,
            "target_comp_id": self.target_comp_id,
            "ssl_enabled": self.ssl_enabled,
            "last_logon_at": self.last_logon_at.isoformat() if self.last_logon_at else None,
            "last_heartbeat_at": (
                self.last_heartbeat_at.isoformat() if self.last_heartbeat_at else None
            ),
            "last_quote_at": self.last_quote_at.isoformat() if self.last_quote_at else None,
            "last_error": self.last_error,
            "outbound_seq": self.outbound_seq,
            "inbound_seq": self.inbound_seq,
            "subscribed_symbol": self.subscribed_symbol,
            "md_req_id": self.md_req_id,
            "warnings": list(self.warnings),
        }


class FixQuoteStore:
    """Thread-safe singleton for latest FIX quotes and session health."""

    _instance: "FixQuoteStore | None" = None
    _lock = threading.Lock()

    def __init__(self) -> None:
        self._quotes: dict[str, FixQuote] = {}
        self._health = FixSessionHealth()
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

    def update_quote(
        self,
        symbol: str,
        *,
        bid: float | None = None,
        ask: float | None = None,
    ) -> FixQuote:
        with self._data_lock:
            existing = self._quotes.get(symbol)
            new_bid = bid if bid is not None else (existing.bid if existing else None)
            new_ask = ask if ask is not None else (existing.ask if existing else None)
            spread = None
            if new_bid is not None and new_ask is not None:
                spread = round(new_ask - new_bid, 6)
            quote = FixQuote(
                symbol=symbol,
                bid=new_bid,
                ask=new_ask,
                spread=spread,
                updated_at=_utcnow(),
            )
            self._quotes[symbol] = quote
            self._health.last_quote_at = quote.updated_at
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

    def diagnostics(self, *, primary_symbol: str | None = None) -> dict[str, Any]:
        with self._data_lock:
            health = self._health.to_dict()
            quote = None
            if primary_symbol and primary_symbol in self._quotes:
                quote = self._quotes[primary_symbol].to_dict()
            elif self._quotes:
                quote = next(iter(self._quotes.values())).to_dict()
            return {
                "session": health,
                "quote": quote,
                "quote_count": len(self._quotes),
            }
