"""Load normalized research snapshots for similarity search."""

from __future__ import annotations

import logging
from datetime import date

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import ResearchMarketSnapshot

logger = logging.getLogger(__name__)
SAMPLE_QUALITIES = {"sample", "mock", "unknown", ""}
COMPARABLE_MIN_SIMILARITY = 0.35


def has_research_snapshots(db: Session) -> bool:
    try:
        n = db.execute(select(func.count(ResearchMarketSnapshot.id)).where(func.lower(ResearchMarketSnapshot.source_quality).notin_(SAMPLE_QUALITIES))).scalar()
        return (n or 0) > 0
    except Exception:  # noqa: BLE001
        logger.exception("research snapshot count failed")
        return False


def research_snapshot_bounds(db: Session) -> dict:
    try:
        start = db.execute(select(func.min(ResearchMarketSnapshot.trade_date))).scalar()
        end = db.execute(select(func.max(ResearchMarketSnapshot.trade_date))).scalar()
        total = db.execute(select(func.count(ResearchMarketSnapshot.id))).scalar() or 0
        return {"start_date": start.isoformat() if start else None, "end_date": end.isoformat() if end else None, "total_snapshots": total}
    except Exception:  # noqa: BLE001
        return {"start_date": None, "end_date": None, "total_snapshots": 0}


def snapshot_to_comparable(snap: ResearchMarketSnapshot) -> dict:
    events = snap.economic_events or []
    primary = events[0] if events else {}
    event_type = primary.get("type") if isinstance(primary, dict) else "market_environment"
    event_name = primary.get("name") if isinstance(primary, dict) else "Daily market environment"
    rate_differential = float(snap.banxico_rate) - float(snap.fed_funds) if snap.banxico_rate is not None and snap.fed_funds is not None else None
    spread_2y = float(snap.mx2y) - float(snap.us2y) if snap.mx2y is not None and snap.us2y is not None else None
    spread_10y = float(snap.mx10y) - float(snap.us10y) if snap.mx10y is not None and snap.us10y is not None else None
    release = snap.trade_date.isoformat()
    return {
        "id": snap.id,
        "event_id": snap.id,
        "event_type": event_type or "market_environment",
        "event_name": event_name or f"Market environment {release}",
        "country": "US",
        "release_time": release,
        "importance": primary.get("importance") if isinstance(primary, dict) else "medium",
        "baseline_price": snap.usdmxn,
        "windows": {"1d": snap.ret_next_1d, "5d": snap.ret_next_5d, "30d": snap.ret_next_30d, "3d": snap.ret_next_5d},
        "max_favorable_excursion": abs(snap.ret_next_5d) if snap.ret_next_5d else None,
        "max_adverse_excursion": abs(snap.ret_next_5d) if snap.ret_next_5d else None,
        "time_to_peak_hours": 24.0 if snap.ret_next_1d is not None else None,
        "reversal_behavior": "unknown",
        "data_completeness": 1.0 if snap.ret_next_1d is not None else 0.5,
        "context": {
            "dxy": snap.dxy, "us2y": snap.us2y, "us10y": snap.us10y,
            "mx2y": snap.mx2y, "mx10y": snap.mx10y,
            "spread_2y": spread_2y, "spread_10y": spread_10y,
            "oil": snap.oil, "gold": snap.gold, "vix": snap.vix, "sp_futures": snap.sp500,
            "momentum": snap.momentum_5d,
            "momentum_1h": snap.momentum_1h, "momentum_2h": snap.momentum_2h, "momentum_4h": snap.momentum_4h,
            "intraday_vol_4h": snap.intraday_vol_4h, "intraday_vol_24h": snap.intraday_vol_24h,
            "iv_1w": snap.iv_1w, "iv_1m": snap.iv_1m,
            "rr25_1w": snap.rr25_1w, "rr25_1m": snap.rr25_1m,
            "butterfly_25d_1m": snap.butterfly_25d_1m,
            "cftc_mxn_net": snap.cftc_mxn_net,
            "cftc_mxn_net_pct_oi": snap.cftc_mxn_net_pct_oi,
            "cftc_mxn_open_interest": snap.cftc_mxn_open_interest,
            "fed_funds": snap.fed_funds, "banxico_rate": snap.banxico_rate,
            "rate_differential": rate_differential, "regime": snap.regime, "news_tags": [],
        },
        "source": snap.source, "source_quality": snap.source_quality, "trade_date": release,
    }


def load_research_comparables(db: Session, *, since: date | None = None, limit: int = 5000) -> list[dict]:
    try:
        stmt = select(ResearchMarketSnapshot).order_by(ResearchMarketSnapshot.trade_date.desc())
        if since:
            stmt = stmt.where(ResearchMarketSnapshot.trade_date >= since)
        stmt = stmt.where(func.lower(ResearchMarketSnapshot.source_quality).notin_(SAMPLE_QUALITIES))
        rows = db.execute(stmt.limit(limit)).scalars().all()
        return [snapshot_to_comparable(r) for r in rows]
    except Exception:  # noqa: BLE001
        logger.exception("load_research_comparables failed")
        return []
