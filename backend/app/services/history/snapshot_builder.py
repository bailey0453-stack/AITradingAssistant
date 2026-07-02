"""Build normalized daily research snapshots from imported series bars."""

from __future__ import annotations

import logging
import math
from collections import defaultdict
from datetime import date, datetime, timezone
from typing import Iterable

from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from app.models import HistoricalMarketSnapshot, ResearchMarketSnapshot

logger = logging.getLogger(__name__)

# Map raw ``historical_market_snapshots.series`` -> research snapshot column.
_SERIES_COLUMN = {
    "USDMXN": "usdmxn",
    "USDMXN_1H": "usdmxn",
    "DXY": "dxy",
    "US2Y": "us2y",
    "US10Y": "us10y",
    "SP_FUTURES": "sp500",
    "SP500": "sp500",
    "VIX": "vix",
    "GOLD": "gold",
    "OIL": "oil",
    "FED_FUNDS": "fed_funds",
    "BANXICO_RATE": "banxico_rate",
    "US_CPI": "us_cpi",
    "MEXICO_CPI": "mexico_cpi",
    "US_PCE": "us_pce",
}

# Also read values already stored in typed columns on raw rows.
_TYPED_COLUMNS = (
    "usdmxn", "dxy", "us2y", "us10y", "oil", "gold", "vix", "sp_futures",
)


def _as_date(ts: datetime) -> date:
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return ts.date()


def _classify_regime(row: dict) -> str | None:
    """Lightweight offline regime tag from macro levels (deterministic)."""
    vix = row.get("vix")
    mom = row.get("momentum_5d")
    us2y = row.get("us2y")
    oil = row.get("oil")

    if vix is not None and vix >= 22:
        return "High Volatility"
    if vix is not None and vix < 14:
        return "Low Volatility"
    if mom is not None and abs(mom) >= 0.08:
        return "Trending"
    if us2y is not None and us2y >= 4.5:
        return "Fed Driven"
    if oil is not None and oil >= 85:
        return "Oil Driven"
    if mom is not None and abs(mom) < 0.02:
        return "Range Bound"
    return "Trending"


def _pct_return(from_price: float | None, to_price: float | None) -> float | None:
    if from_price is None or to_price is None or from_price == 0:
        return None
    return round((to_price / from_price - 1.0) * 100.0, 4)


def _volatility(returns: list[float]) -> float | None:
    if len(returns) < 2:
        return None
    mean = sum(returns) / len(returns)
    var = sum((r - mean) ** 2 for r in returns) / (len(returns) - 1)
    return round(math.sqrt(var), 4)


def _load_daily_panel(db: Session) -> dict[date, dict]:
    """Aggregate raw series bars into one dict per calendar day (last value wins)."""
    panel: dict[date, dict] = defaultdict(dict)

    def _merge(ts: datetime, column: str, value: float | None) -> None:
        if value is None:
            return
        day = _as_date(ts)
        panel[day][column] = float(value)

    rows = db.execute(
        select(HistoricalMarketSnapshot).order_by(HistoricalMarketSnapshot.ts.asc())
    ).scalars().all()

    for row in rows:
        series_key = (row.series or "").upper()
        col = _SERIES_COLUMN.get(series_key)
        if col:
            attr = "sp_futures" if col == "sp500" else col
            val = getattr(row, attr, None)
            if val is not None:
                _merge(row.ts, col, val)
        for typed in _TYPED_COLUMNS:
            val = getattr(row, typed, None)
            if val is None:
                continue
            target = "sp500" if typed == "sp_futures" else typed
            _merge(row.ts, target, val)

    return panel


def _events_for_day(db: Session, day: date) -> list[dict]:
    from app.models import HistoricalEvent

    start = datetime(day.year, day.month, day.day, tzinfo=timezone.utc)
    end = start.replace(hour=23, minute=59, second=59)
    rows = db.execute(
        select(HistoricalEvent)
        .where(HistoricalEvent.release_time >= start)
        .where(HistoricalEvent.release_time <= end)
        .order_by(HistoricalEvent.release_time.asc())
    ).scalars().all()
    return [
        {
            "type": e.event_type,
            "name": e.event_name,
            "importance": e.importance,
            "surprise_z": e.surprise_z,
        }
        for e in rows
    ]


def build_research_snapshots(
    db: Session,
    *,
    min_date: date | None = None,
    replace: bool = False,
    source: str = "research",
    source_quality: str = "imported",
) -> dict:
    """Merge imported series into ``research_market_snapshots`` + derived metrics."""
    panel = _load_daily_panel(db)
    if not panel:
        return {"snapshots": 0, "reason": "no raw series data"}

    if replace:
        db.execute(delete(ResearchMarketSnapshot))
        db.commit()

    usd_days = sorted(d for d, row in panel.items() if row.get("usdmxn") is not None)
    if min_date:
        usd_days = [d for d in usd_days if d >= min_date]
    if not usd_days:
        return {"snapshots": 0, "reason": "no USD/MXN observations"}

    # Forward-fill macro fields onto USD/MXN trading days.
    last: dict[str, float] = {}
    filled: dict[date, dict] = {}
    for day in sorted(panel.keys()):
        row = dict(panel[day])
        for key, val in row.items():
            if val is not None:
                last[key] = val
        for key, val in last.items():
            row.setdefault(key, val)
        filled[day] = row

    prices = {d: filled[d]["usdmxn"] for d in usd_days if filled[d].get("usdmxn") is not None}
    daily_rets: dict[date, float] = {}
    ordered = usd_days
    for i, day in enumerate(ordered):
        if i == 0:
            continue
        prev = ordered[i - 1]
        r = _pct_return(prices.get(prev), prices.get(day))
        if r is not None:
            daily_rets[day] = r

    existing = {
        r.trade_date: r
        for r in db.execute(select(ResearchMarketSnapshot)).scalars().all()
    }

    created = updated = 0
    for i, day in enumerate(ordered):
        row = filled.get(day, {})
        price = prices.get(day)
        if price is None:
            continue

        def _fwd(days_ahead: int) -> float | None:
            if i + days_ahead >= len(ordered):
                return None
            future = ordered[i + days_ahead]
            return _pct_return(price, prices.get(future))

        rets_window = [
            daily_rets[d] for d in ordered[max(0, i - 19): i + 1] if d in daily_rets
        ]
        mom5 = _pct_return(prices.get(ordered[max(0, i - 5)]), price) if i >= 5 else None
        mom20 = _pct_return(prices.get(ordered[max(0, i - 20)]), price) if i >= 20 else None

        payload = {
            "usdmxn": price,
            "dxy": row.get("dxy"),
            "us2y": row.get("us2y"),
            "us10y": row.get("us10y"),
            "sp500": row.get("sp500"),
            "vix": row.get("vix"),
            "gold": row.get("gold"),
            "oil": row.get("oil"),
            "fed_funds": row.get("fed_funds"),
            "banxico_rate": row.get("banxico_rate"),
            "us_cpi": row.get("us_cpi"),
            "mexico_cpi": row.get("mexico_cpi"),
            "us_pce": row.get("us_pce"),
            "momentum_5d": mom5,
            "momentum_20d": mom20,
            "volatility_20d": _volatility(rets_window),
            "ret_next_1d": _fwd(1),
            "ret_next_5d": _fwd(5),
            "ret_next_30d": _fwd(30),
            "economic_events": _events_for_day(db, day) or None,
            "source": source,
            "source_quality": source_quality,
        }
        payload["regime"] = _classify_regime({**payload, "momentum_5d": mom5})

        snap = existing.get(day)
        if snap:
            for key, val in payload.items():
                setattr(snap, key, val)
            updated += 1
        else:
            db.add(ResearchMarketSnapshot(trade_date=day, **payload))
            created += 1

    db.commit()
    total = db.execute(select(func.count(ResearchMarketSnapshot.id))).scalar() or 0
    return {
        "snapshots": total,
        "created": created,
        "updated": updated,
        "start_date": ordered[0].isoformat(),
        "end_date": ordered[-1].isoformat(),
    }
