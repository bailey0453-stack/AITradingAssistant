"""Backfill hourly USD/MXN history and derive intraday research features.

Yahoo Finance's public chart API exposes roughly two years of 1-hour FX bars.
Those bars are stored as ``USDMXN_1H`` rows in ``historical_market_snapshots``
and are used to derive 1h/2h/4h momentum, 4h/24h realized volatility, and
1h/2h/4h forward returns for short-horizon historical analog forecasts.

The repair is idempotent and safe to run from the hourly scheduler. It does not
alter recommendation logic when the provider is unavailable; missing intraday
features are simply skipped by the similarity engine.
"""

from __future__ import annotations

import math
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.models import HistoricalMarketSnapshot, ResearchMarketSnapshot

YAHOO_URL = "https://query1.finance.yahoo.com/v8/finance/chart/MXN=X"
SERIES = "USDMXN_1H"
MAX_LOOKBACK_DAYS = 729


def _aware(ts: datetime) -> datetime:
    return ts if ts.tzinfo else ts.replace(tzinfo=timezone.utc)


def _hour_bucket(ts: datetime) -> datetime:
    return _aware(ts).replace(minute=0, second=0, microsecond=0)


def _pct(a: float | None, b: float | None) -> float | None:
    if a is None or b is None or a == 0:
        return None
    return round((b / a - 1.0) * 100.0, 5)


def _vol(values: list[float]) -> float | None:
    if len(values) < 2:
        return None
    returns = []
    for i in range(1, len(values)):
        r = _pct(values[i - 1], values[i])
        if r is not None:
            returns.append(r)
    if len(returns) < 2:
        return None
    mean = sum(returns) / len(returns)
    var = sum((r - mean) ** 2 for r in returns) / (len(returns) - 1)
    return round(math.sqrt(var), 5)


def _fetch_hourly() -> list[tuple[datetime, float]]:
    settings = get_settings()
    now = datetime.now(timezone.utc)
    params = {
        "interval": "1h",
        "period1": int((now - timedelta(days=MAX_LOOKBACK_DAYS)).timestamp()),
        "period2": int(now.timestamp()),
        "includePrePost": "false",
        "events": "div,splits",
    }
    response = httpx.get(
        YAHOO_URL,
        params=params,
        timeout=max(float(settings.http_timeout_seconds), 12.0),
        headers={"User-Agent": "AITradingAssistant/1.0"},
    )
    response.raise_for_status()
    result = ((response.json() or {}).get("chart") or {}).get("result") or []
    if not result:
        return []
    block = result[0]
    timestamps = block.get("timestamp") or []
    closes = (((block.get("indicators") or {}).get("quote") or [{}])[0].get("close") or [])
    out: list[tuple[datetime, float]] = []
    for raw_ts, close in zip(timestamps, closes):
        if close is None:
            continue
        out.append((datetime.fromtimestamp(int(raw_ts), tz=timezone.utc), float(close)))
    return out


def _persist_hourly(db: Session, observations: list[tuple[datetime, float]]) -> int:
    if not observations:
        return 0
    start = min(ts for ts, _ in observations) - timedelta(hours=1)
    end = max(ts for ts, _ in observations) + timedelta(hours=1)
    rows = db.execute(
        select(HistoricalMarketSnapshot).where(
            HistoricalMarketSnapshot.series == SERIES,
            HistoricalMarketSnapshot.ts >= start,
            HistoricalMarketSnapshot.ts <= end,
        )
    ).scalars().all()
    existing = {_hour_bucket(row.ts): row for row in rows}
    changed = 0
    for ts, value in observations:
        key = _hour_bucket(ts)
        row = existing.get(key)
        if row is None:
            row = HistoricalMarketSnapshot(
                series=SERIES,
                ts=key,
                usdmxn=value,
                value=value,
                source="yahoo",
                source_quality="vendor_free",
            )
            db.add(row)
            existing[key] = row
            changed += 1
        elif row.value != value or row.usdmxn != value:
            row.value = value
            row.usdmxn = value
            changed += 1
    if changed:
        db.commit()
    return changed


def _derive_features(db: Session) -> dict:
    rows = db.execute(
        select(HistoricalMarketSnapshot)
        .where(HistoricalMarketSnapshot.series == SERIES)
        .order_by(HistoricalMarketSnapshot.ts.asc())
    ).scalars().all()
    bars: list[tuple[datetime, float]] = []
    for row in rows:
        value = row.value if row.value is not None else row.usdmxn
        if value is not None:
            bars.append((_aware(row.ts), float(value)))
    if len(bars) < 5:
        return {"snapshots_updated": 0, "feature_days": 0, "forward_return_days": 0}

    by_day: dict[date, list[int]] = defaultdict(list)
    for idx, (ts, _value) in enumerate(bars):
        by_day[ts.date()].append(idx)

    snapshots = {
        row.trade_date: row
        for row in db.execute(
            select(ResearchMarketSnapshot).where(
                ResearchMarketSnapshot.trade_date >= bars[0][0].date(),
                ResearchMarketSnapshot.trade_date <= bars[-1][0].date(),
            )
        ).scalars().all()
    }

    updated = feature_days = forward_return_days = 0
    for day, indices in by_day.items():
        snap = snapshots.get(day)
        if snap is None or not indices:
            continue
        i = indices[-1]
        current = bars[i][1]

        def back(hours: int) -> float | None:
            j = i - hours
            return bars[j][1] if j >= 0 else None

        def forward(hours: int) -> float | None:
            j = i + hours
            return bars[j][1] if j < len(bars) else None

        m1 = _pct(back(1), current)
        m2 = _pct(back(2), current)
        m4 = _pct(back(4), current)
        vals4 = [v for _ts, v in bars[max(0, i - 4): i + 1]]
        vals24 = [v for _ts, v in bars[max(0, i - 24): i + 1]]
        v4 = _vol(vals4)
        v24 = _vol(vals24)
        f1 = _pct(current, forward(1))
        f2 = _pct(current, forward(2))
        f4 = _pct(current, forward(4))

        values = {
            "momentum_1h": m1,
            "momentum_2h": m2,
            "momentum_4h": m4,
            "intraday_vol_4h": v4,
            "intraday_vol_24h": v24,
            "ret_next_1h": f1,
            "ret_next_2h": f2,
            "ret_next_4h": f4,
        }
        if any(values[k] is not None for k in ("momentum_1h", "momentum_2h", "momentum_4h", "intraday_vol_4h", "intraday_vol_24h")):
            feature_days += 1
        if any(values[k] is not None for k in ("ret_next_1h", "ret_next_2h", "ret_next_4h")):
            forward_return_days += 1
        dirty = False
        for key, value in values.items():
            if value is not None and getattr(snap, key) != value:
                setattr(snap, key, value)
                dirty = True
        if dirty:
            updated += 1

    if updated:
        db.commit()
    return {
        "snapshots_updated": updated,
        "feature_days": feature_days,
        "forward_return_days": forward_return_days,
    }


def current_intraday_features(db: Session) -> dict:
    rows = db.execute(
        select(HistoricalMarketSnapshot)
        .where(HistoricalMarketSnapshot.series == SERIES)
        .order_by(HistoricalMarketSnapshot.ts.desc())
        .limit(25)
    ).scalars().all()
    values = []
    for row in reversed(rows):
        value = row.value if row.value is not None else row.usdmxn
        if value is not None:
            values.append(float(value))
    if len(values) < 5:
        return {}
    current = values[-1]
    out = {
        "momentum_1h": _pct(values[-2], current),
        "momentum_2h": _pct(values[-3], current) if len(values) >= 3 else None,
        "momentum_4h": _pct(values[-5], current) if len(values) >= 5 else None,
        "intraday_vol_4h": _vol(values[-5:]),
        "intraday_vol_24h": _vol(values[-25:]),
    }
    return {k: v for k, v in out.items() if v is not None}


def repair_intraday_usdmxn(db: Session) -> dict:
    try:
        observations = _fetch_hourly()
    except Exception as exc:  # noqa: BLE001 - provider failure must be non-fatal
        return {"ok": False, "reason": str(exc), "source": "Yahoo MXN=X 1h"}
    changed = _persist_hourly(db, observations)
    derived = _derive_features(db)
    return {
        "ok": True,
        "source": "Yahoo MXN=X 1h",
        "observations": len(observations),
        "bars_changed": changed,
        **derived,
    }
