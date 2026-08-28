"""Repair policy-rate history used by research snapshots.

Fed Funds is sourced from FRED DFF (daily effective federal funds rate).
Banxico target-rate history is sourced from Banco de Mexico SIE series SF61745
when BANXICO_API_TOKEN is configured.  Values are forward-filled across the
existing USD/MXN research trading days so the rate differential is available to
historical similarity/regime analysis.
"""

from __future__ import annotations

from datetime import date, datetime, timezone

import httpx
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.models import ResearchMarketSnapshot

FRED_OBSERVATIONS_URL = "https://api.stlouisfed.org/fred/series/observations"
BANXICO_SERIES_URL = "https://www.banxico.org.mx/SieAPIRest/service/v1/series/SF61745/datos/{start}/{end}"


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


def _research_bounds(db: Session) -> tuple[date | None, date | None]:
    start, end = db.execute(
        select(
            func.min(ResearchMarketSnapshot.trade_date),
            func.max(ResearchMarketSnapshot.trade_date),
        )
    ).one()
    return start, end


def _fill_column(db: Session, column_name: str, observations: dict[date, float]) -> int:
    if not observations:
        return 0
    rows = db.execute(
        select(ResearchMarketSnapshot).order_by(ResearchMarketSnapshot.trade_date.asc())
    ).scalars().all()
    ordered_obs = sorted(observations.items())
    idx = 0
    current: float | None = None
    updated = 0
    for row in rows:
        while idx < len(ordered_obs) and ordered_obs[idx][0] <= row.trade_date:
            current = ordered_obs[idx][1]
            idx += 1
        if current is None:
            continue
        if getattr(row, column_name) != current:
            setattr(row, column_name, current)
            updated += 1
    if updated:
        db.commit()
    return updated


def repair_fed_funds(db: Session) -> dict:
    settings = get_settings()
    if not settings.fred_api_key:
        return {"ok": False, "reason": "FRED_API_KEY not configured"}
    start, end = _research_bounds(db)
    if not start or not end:
        return {"ok": False, "reason": "no research snapshots"}
    params = {
        "series_id": "DFF",
        "api_key": settings.fred_api_key,
        "file_type": "json",
        "observation_start": start.isoformat(),
        "observation_end": end.isoformat(),
    }
    response = httpx.get(FRED_OBSERVATIONS_URL, params=params, timeout=settings.http_timeout_seconds)
    response.raise_for_status()
    observations: dict[date, float] = {}
    for item in (response.json() or {}).get("observations", []):
        value = _parse_number(item.get("value"))
        if value is None:
            continue
        observations[date.fromisoformat(item["date"])] = value
    updated = _fill_column(db, "fed_funds", observations)
    return {"ok": True, "source": "FRED DFF", "observations": len(observations), "snapshots_updated": updated}


def repair_banxico_rate(db: Session) -> dict:
    settings = get_settings()
    token = getattr(settings, "banxico_api_token", None)
    if not token:
        return {"ok": False, "reason": "BANXICO_API_TOKEN not configured", "series": "SF61745"}
    start, end = _research_bounds(db)
    if not start or not end:
        return {"ok": False, "reason": "no research snapshots"}
    url = BANXICO_SERIES_URL.format(start=start.isoformat(), end=end.isoformat())
    response = httpx.get(url, headers={"Bmx-Token": token}, timeout=max(settings.http_timeout_seconds, 12.0))
    response.raise_for_status()
    observations: dict[date, float] = {}
    series = ((response.json() or {}).get("bmx") or {}).get("series") or []
    for entry in series:
        if str(entry.get("idSerie", "")).upper() != "SF61745":
            continue
        for item in entry.get("datos") or []:
            value = _parse_number(item.get("dato"))
            if value is None:
                continue
            raw_date = str(item.get("fecha") or "")
            try:
                day = datetime.strptime(raw_date, "%d/%m/%Y").date()
            except ValueError:
                continue
            observations[day] = value
    updated = _fill_column(db, "banxico_rate", observations)
    return {"ok": True, "source": "Banco de Mexico SIE SF61745", "observations": len(observations), "snapshots_updated": updated}


def repair_policy_rates(db: Session) -> dict:
    fed = repair_fed_funds(db)
    banxico = repair_banxico_rate(db)
    return {"fed_funds": fed, "banxico_rate": banxico}
