"""Backfill Mexico 2Y/10Y sovereign yields into research snapshots.

Uses Banco de Mexico SIE when BANXICO_API_TOKEN plus the configured series IDs
are available. Missing source configuration is non-fatal; the similarity engine
simply skips these features until data exists.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta

import httpx
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.models import ResearchMarketSnapshot

BANXICO_SERIES_URL = "https://www.banxico.org.mx/SieAPIRest/service/v1/series/{series}/datos/{start}/{end}"


def _parse_number(value) -> float | None:
    if value is None:
        return None
    text = str(value).strip().replace(",", "")
    if not text or text in {".", "N/E", "N/D", "NA"}:
        return None
    try:
        return float(text)
    except ValueError:
        return None


def _missing_bounds(db: Session, column_name: str) -> tuple[date | None, date | None, int]:
    column = getattr(ResearchMarketSnapshot, column_name)
    start, end, count = db.execute(
        select(
            func.min(ResearchMarketSnapshot.trade_date),
            func.max(ResearchMarketSnapshot.trade_date),
            func.count(ResearchMarketSnapshot.id),
        ).where(column.is_(None))
    ).one()
    return start, end, int(count or 0)


def _fetch(series_id: str, start: date, end: date, token: str, timeout: float) -> dict[date, float]:
    url = BANXICO_SERIES_URL.format(
        series=series_id,
        start=(start - timedelta(days=14)).isoformat(),
        end=end.isoformat(),
    )
    response = httpx.get(url, headers={"Bmx-Token": token}, timeout=max(timeout, 12.0))
    response.raise_for_status()
    observations: dict[date, float] = {}
    for series in ((response.json() or {}).get("bmx") or {}).get("series") or []:
        for item in series.get("datos") or []:
            value = _parse_number(item.get("dato"))
            raw_date = str(item.get("fecha") or "")
            if value is None:
                continue
            try:
                day = datetime.strptime(raw_date, "%d/%m/%Y").date()
            except ValueError:
                continue
            observations[day] = value
    return observations


def _fill(db: Session, column_name: str, observations: dict[date, float]) -> int:
    if not observations:
        return 0
    rows = db.execute(
        select(ResearchMarketSnapshot).order_by(ResearchMarketSnapshot.trade_date.asc())
    ).scalars().all()
    ordered = sorted(observations.items())
    idx = 0
    current: float | None = None
    updated = 0
    for row in rows:
        while idx < len(ordered) and ordered[idx][0] <= row.trade_date:
            current = ordered[idx][1]
            idx += 1
        if current is None or getattr(row, column_name) is not None:
            continue
        setattr(row, column_name, current)
        updated += 1
    if updated:
        db.commit()
    return updated


def _repair_one(db: Session, *, column: str, series_id: str | None, label: str) -> dict:
    start, end, missing = _missing_bounds(db, column)
    if missing == 0:
        return {"ok": True, "skipped": True, "reason": f"{label} coverage already complete"}
    settings = get_settings()
    token = getattr(settings, "banxico_api_token", None)
    if not token:
        return {"ok": False, "reason": "BANXICO_API_TOKEN not configured", "missing": missing}
    if not series_id:
        return {"ok": False, "reason": f"Banxico series ID for {label} not configured", "missing": missing}
    if start is None or end is None:
        return {"ok": False, "reason": "no research snapshots"}
    observations = _fetch(series_id, start, end, token, settings.http_timeout_seconds)
    updated = _fill(db, column, observations)
    _, _, remaining = _missing_bounds(db, column)
    return {
        "ok": True,
        "source": f"Banco de Mexico SIE {series_id}",
        "observations": len(observations),
        "snapshots_updated": updated,
        "snapshots_missing_after": remaining,
    }


def repair_mexico_yields(db: Session) -> dict:
    settings = get_settings()
    return {
        "mx2y": _repair_one(
            db,
            column="mx2y",
            series_id=getattr(settings, "banxico_mx2y_series_id", None),
            label="Mexico 2Y yield",
        ),
        "mx10y": _repair_one(
            db,
            column="mx10y",
            series_id=getattr(settings, "banxico_mx10y_series_id", None),
            label="Mexico 10Y yield",
        ),
    }
