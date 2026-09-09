"""Ingest USD/MXN options volatility/skew observations into research snapshots.

The feed is intentionally provider-agnostic. Configure ``FX_OPTIONS_DATA_URL``
and optionally ``FX_OPTIONS_API_KEY``. The endpoint may return either a single
observation or ``{"observations": [...]}``.

Accepted fields (aliases in parentheses):
- date (trade_date, timestamp)
- iv_1w (implied_vol_1w)
- iv_1m (implied_vol_1m)
- rr25_1w (risk_reversal_25d_1w)
- rr25_1m (risk_reversal_25d_1m)
- butterfly_25d_1m (bf25_1m, butterfly_1m)

Missing/failed feeds never fabricate values and never block forecasting.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.models import ResearchMarketSnapshot

_FIELDS = {
    "iv_1w": ("iv_1w", "implied_vol_1w"),
    "iv_1m": ("iv_1m", "implied_vol_1m"),
    "rr25_1w": ("rr25_1w", "risk_reversal_25d_1w"),
    "rr25_1m": ("rr25_1m", "risk_reversal_25d_1m"),
    "butterfly_25d_1m": ("butterfly_25d_1m", "bf25_1m", "butterfly_1m"),
}


def _num(item: dict, aliases: tuple[str, ...]) -> float | None:
    for key in aliases:
        value = item.get(key)
        if value is None or value == "":
            continue
        try:
            return float(value)
        except (TypeError, ValueError):
            continue
    return None


def _day(item: dict) -> date | None:
    raw = item.get("date") or item.get("trade_date") or item.get("timestamp")
    if not raw:
        return None
    text = str(raw).strip().replace("Z", "+00:00")
    try:
        return date.fromisoformat(text[:10])
    except ValueError:
        try:
            return datetime.fromisoformat(text).date()
        except ValueError:
            return None


def _fetch() -> list[dict[str, Any]]:
    settings = get_settings()
    if not settings.fx_options_data_url:
        return []
    headers = {"Accept": "application/json"}
    if settings.fx_options_api_key:
        headers["Authorization"] = f"Bearer {settings.fx_options_api_key}"
    response = httpx.get(
        settings.fx_options_data_url,
        headers=headers,
        timeout=max(settings.http_timeout_seconds, 12.0),
    )
    response.raise_for_status()
    payload = response.json()
    if isinstance(payload, list):
        return [x for x in payload if isinstance(x, dict)]
    if isinstance(payload, dict):
        observations = payload.get("observations") or payload.get("data")
        if isinstance(observations, list):
            return [x for x in observations if isinstance(x, dict)]
        return [payload]
    return []


def repair_fx_options(db: Session) -> dict:
    settings = get_settings()
    if not settings.fx_options_data_url:
        return {"ok": False, "skipped": True, "reason": "FX_OPTIONS_DATA_URL not configured"}

    observations = _fetch()
    parsed: dict[date, dict[str, float]] = {}
    for item in observations:
        day = _day(item)
        if not day:
            continue
        values = {field: _num(item, aliases) for field, aliases in _FIELDS.items()}
        values = {k: v for k, v in values.items() if v is not None}
        if values:
            parsed[day] = values

    if not parsed:
        return {"ok": False, "reason": "options feed returned no usable observations"}

    rows = db.execute(
        select(ResearchMarketSnapshot).order_by(ResearchMarketSnapshot.trade_date.asc())
    ).scalars().all()
    ordered = sorted(parsed.items())
    idx = 0
    current: dict[str, float] = {}
    updated = 0
    for row in rows:
        while idx < len(ordered) and ordered[idx][0] <= row.trade_date:
            current.update(ordered[idx][1])
            idx += 1
        for field, value in current.items():
            if getattr(row, field, None) != value:
                setattr(row, field, value)
                updated += 1
    if updated:
        db.commit()

    latest_day, latest = ordered[-1]
    return {
        "ok": True,
        "source": settings.fx_options_data_url,
        "observations": len(parsed),
        "latest_date": latest_day.isoformat(),
        "latest": latest,
        "snapshot_fields_updated": updated,
    }


def current_fx_options(db: Session) -> dict[str, float | None]:
    row = db.execute(
        select(ResearchMarketSnapshot)
        .where(ResearchMarketSnapshot.iv_1w.isnot(None) | ResearchMarketSnapshot.iv_1m.isnot(None))
        .order_by(ResearchMarketSnapshot.trade_date.desc())
        .limit(1)
    ).scalars().first()
    if not row:
        return {field: None for field in _FIELDS}
    return {field: getattr(row, field, None) for field in _FIELDS}
