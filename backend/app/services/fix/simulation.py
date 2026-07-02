"""Simulation-only execution objects — never sent over FIX in Phase 1."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Literal
from uuid import uuid4

SimSide = Literal["BUY", "SELL"]
SimStatus = Literal["simulation_only", "rejected_simulation"]


@dataclass
class SimulatedOrder:
    """Desk-style order object for future phases — not transmitted live."""

    symbol: str
    side: SimSide
    quantity: float
    order_id: str = field(default_factory=lambda: f"SIM-{uuid4().hex[:12]}")
    status: SimStatus = "simulation_only"
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    note: str = "Phase 1 — simulation only; no NewOrderSingle sent."

    def to_dict(self) -> dict:
        return {
            "order_id": self.order_id,
            "symbol": self.symbol,
            "side": self.side,
            "quantity": self.quantity,
            "status": self.status,
            "created_at": self.created_at.isoformat(),
            "note": self.note,
        }


def build_simulated_order(symbol: str, side: SimSide, quantity: float) -> SimulatedOrder:
    """Create a simulation-only order (never placed on Centroid/GFC)."""
    return SimulatedOrder(symbol=symbol, side=side, quantity=quantity)
