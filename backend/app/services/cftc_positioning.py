"""Free CFTC Mexican peso positioning for USD/MXN research.

Uses the CFTC Public Reporting Environment (Socrata) Legacy Futures Only dataset.
No API key or paid market-data license is required.
"""

from __future__ import annotations

from datetime import date
from typing import Any

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.models import ResearchMarketSnapshot

CFTC_URL = "https://publicreporting.cftc.gov/resource/6dca-aqww.json"


def _f(row: dict[str, Any], key: str) -> float | None:
    value = row.get(key)
    if value in (None, ""):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _day(row: dict[str, Any]) -> date | None:
    raw = row.get("report_date_as_yyyy_mm_dd")
    if not raw:
        return None
    try:
        return date.fromisoformat(str(raw)[:10])
    except ValueError:
        return None


def fetch_mexican_peso_positioning() -> list[dict[str, float | date | None]]:
    """Fetch the full Mexican peso COT history from the official CFTC API."""
    params = {
        "$select": (
            "report_date_as_yyyy_mm_dd,open_interest_all,"
            "noncomm_positions_long_all,noncomm_positions_short_all"
        ),
        "$where": "contract_market_name='MEXICAN PESO'",
        "$order": "report_date_as_yyyy_mm_dd asc",
        "$limit": "10000",
    }
    timeout = max(get_settings().http_timeout_seconds, 15.0)
    response = httpx.get(CFTC_URL, params=params, timeout=timeout)
    response.raise_for_status()
    payload = response.json()
    if not isinstance(payload, list):
        return []

    out: list[dict[str, float | date | None]] = []
    for row in payload:
        if not isinstance(row, dict):
            continue
        d = _day(row)
        long_pos = _f(row, "noncomm_positions_long_all")
        short_pos = _f(row, "noncomm_positions_short_all")
        oi = _f(row, "open_interest_all")
        if d is None or long_pos is None or short_pos is None:
            continue
        net = long_pos - short_pos
        net_pct_oi = (net / oi * 100.0) if oi else None
        out.append({
            "date": d,
            "cftc_mxn_net": net,
            "cftc_mxn_net_pct_oi": net_pct_oi,
            "cftc_mxn_open_interest": oi,
        })
    return out


def repair_cftc_positioning(db: Session) -> dict:
    """Forward-fill weekly CFTC positioning onto daily research snapshots."""
    observations = fetch_mexican_peso_positioning()
    if not observations:
        return {"ok": False, "reason": "CFTC returned no Mexican peso observations"}

    rows = db.execute(
        select(ResearchMarketSnapshot).order_by(ResearchMarketSnapshot.trade_date.asc())
    ).scalars().all()
    idx = 0
    current: dict[str, float | None] = {}
    updated = 0
    for row in rows:
        while idx < len(observations) and observations[idx]["date"] <= row.trade_date:
            current = {
                "cftc_mxn_net": observations[idx]["cftc_mxn_net"],
                "cftc_mxn_net_pct_oi": observations[idx]["cftc_mxn_net_pct_oi"],
                "cftc_mxn_open_interest": observations[idx]["cftc_mxn_open_interest"],
            }
            idx += 1
        for field, value in current.items():
            if value is not None and getattr(row, field, None) != value:
                setattr(row, field, value)
                updated += 1
    if updated:
        db.commit()

    latest = observations[-1]
    return {
        "ok": True,
        "source": "CFTC Legacy Futures Only",
        "observations": len(observations),
        "latest_date": latest["date"].isoformat(),
        "latest_net": latest["cftc_mxn_net"],
        "latest_net_pct_oi": latest["cftc_mxn_net_pct_oi"],
        "snapshot_fields_updated": updated,
    }


def current_cftc_positioning(db: Session) -> dict[str, float | None]:
    row = db.execute(
        select(ResearchMarketSnapshot)
        .where(ResearchMarketSnapshot.cftc_mxn_net.isnot(None))
        .order_by(ResearchMarketSnapshot.trade_date.desc())
        .limit(1)
    ).scalars().first()
    if not row:
        return {
            "cftc_mxn_net": None,
            "cftc_mxn_net_pct_oi": None,
            "cftc_mxn_open_interest": None,
        }
    return {
        "cftc_mxn_net": row.cftc_mxn_net,
        "cftc_mxn_net_pct_oi": row.cftc_mxn_net_pct_oi,
        "cftc_mxn_open_interest": row.cftc_mxn_open_interest,
    }
