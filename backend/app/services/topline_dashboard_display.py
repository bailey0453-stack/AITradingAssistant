"""Presentation helpers for the Topline Rate Forecast dashboard."""

from __future__ import annotations


def grade_with_hedge_pnl(grade: str | None, pnl_usd: float | None) -> str | None:
    """Return the existing grade plus a compact net $100k hedge P/L label.

    The dashboard already renders each horizon's ``grade`` inside its card. This
    helper keeps the underlying opportunity grade available separately while
    making the FIX-based P/L visible per horizon without changing trade gates.
    """
    if grade is None:
        return None
    if pnl_usd is None:
        return grade
    sign = "+" if pnl_usd >= 0 else "-"
    amount = f"${abs(pnl_usd):,.0f}"
    return f"{grade} · $100K net {sign}{amount}"
