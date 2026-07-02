"""Read-only Research Database Status aggregator for the dashboard.

Collects health metrics from existing tables and services without triggering
imports, evaluations, or analysis. Designed for extensibility: new datasets
register via ``COVERAGE_FIELDS`` and ``EVENT_TYPES``.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.database import database_is_persistent, database_kind
from app.models import (
    HistoricalEvent,
    HistoricalMarketSnapshot,
    JobRun,
    Recommendation,
    RecommendationOutcome,
    ResearchDailyLearning,
    ResearchMarketSnapshot,
)
from app.services import cache_manager, research_lab
from app.services.history.historical_events import history_diagnostics
from app.services.history.historical_snapshots import COMPARABLE_MIN_SIMILARITY
from app.services.history.importers import get_importer
from app.services.scheduled_jobs import job_status
from app.versions import HISTORICAL_ENGINE_VERSION, version_tags

logger = logging.getLogger(__name__)

RESEARCH_DATABASE_VERSION = "1.0"

# Extensible coverage registry: key -> (research column, raw series names).
COVERAGE_FIELDS: dict[str, dict[str, Any]] = {
    "usdmxn": {"label": "USD/MXN", "column": "usdmxn", "series": ["USDMXN", "USDMXN_1H"]},
    "dxy": {"label": "DXY", "column": "dxy", "series": ["DXY"]},
    "gold": {"label": "Gold", "column": "gold", "series": ["GOLD"]},
    "oil": {"label": "Oil", "column": "oil", "series": ["OIL"]},
    "sp500": {"label": "S&P 500", "column": "sp500", "series": ["SP500", "SP_FUTURES"]},
    "vix": {"label": "VIX", "column": "vix", "series": ["VIX"]},
    "us2y": {"label": "US 2Y Treasury", "column": "us2y", "series": ["US2Y"]},
    "us10y": {"label": "US 10Y Treasury", "column": "us10y", "series": ["US10Y"]},
    "fed_funds": {"label": "Fed Funds Rate", "column": "fed_funds", "series": ["FED_FUNDS"]},
    "banxico_rate": {"label": "Banxico Rate", "column": "banxico_rate", "series": ["BANXICO_RATE"]},
}

# Extensible economic event registry.
EVENT_TYPES: dict[str, dict[str, Any]] = {
    "fomc": {"label": "FOMC meetings", "types": ["fed_rate_decision"]},
    "powell_speeches": {"label": "Powell speeches", "types": ["powell_speech"]},
    "cpi": {"label": "CPI releases", "types": ["us_cpi", "mexico_cpi"]},
    "pce": {"label": "PCE releases", "types": ["us_pce"]},
    "nfp": {"label": "NFP releases", "types": ["us_nfp"]},
    "banxico": {"label": "Banxico meetings", "types": ["banxico_rate_decision"]},
}

_STATUS_OK = "current"
_STATUS_WARN = "missing"
_STATUS_UPD = "updating"


def _safe(fn, default=None):
    try:
        return fn()
    except Exception:  # noqa: BLE001
        logger.exception("research_database_status query failed")
        return default


def _iso(val) -> str | None:
    if val is None:
        return None
    if isinstance(val, datetime):
        return val.isoformat()
    return str(val)


def _coverage_status(pct: float | None, rows: int) -> str:
    if rows == 0 or pct is None or pct <= 0:
        return _STATUS_WARN
    if pct >= 0.85:
        return _STATUS_OK
    if pct >= 0.25:
        return _STATUS_UPD
    return _STATUS_WARN


def _field_coverage(db: Session, column: str, series_names: list[str]) -> dict:
    total_research = _safe(
        lambda: db.execute(select(func.count(ResearchMarketSnapshot.id))).scalar() or 0,
        0,
    )
    if total_research:
        col = getattr(ResearchMarketSnapshot, column, None)
        if col is not None:
            filled = _safe(
                lambda: db.execute(
                    select(func.count(ResearchMarketSnapshot.id)).where(col.isnot(None))
                ).scalar()
                or 0,
                0,
            )
            pct = filled / total_research if total_research else 0.0
            return {
                "status": _coverage_status(pct, total_research),
                "coverage_pct": round(pct * 100, 1),
                "filled_days": int(filled),
                "source": "research_market_snapshots",
            }

    # Fallback: raw imported series bars exist?
    raw = 0
    for name in series_names:
        raw += _safe(
            lambda n=name: db.execute(
                select(func.count(HistoricalMarketSnapshot.id)).where(
                    HistoricalMarketSnapshot.series == n
                )
            ).scalar()
            or 0,
            0,
        )
    if raw > 0:
        return {
            "status": _STATUS_UPD,
            "coverage_pct": None,
            "filled_days": raw,
            "source": "historical_market_snapshots",
            "detail": "Raw series imported; not yet merged into daily snapshots.",
        }
    return {"status": _STATUS_WARN, "coverage_pct": 0.0, "filled_days": 0, "source": "none"}


def _event_counts(db: Session) -> dict[str, dict]:
    out: dict[str, dict] = {}
    for key, meta in EVENT_TYPES.items():
        types = meta["types"]
        count = _safe(
            lambda t=types: db.execute(
                select(func.count(HistoricalEvent.id)).where(HistoricalEvent.event_type.in_(t))
            ).scalar()
            or 0,
            0,
        )
        out[key] = {
            "label": meta["label"],
            "count": int(count),
            "status": _STATUS_OK if count > 0 else _STATUS_WARN,
        }
    return out


def _health_level(warnings: list[str], errors: list[str]) -> str:
    if errors:
        return "error"
    if warnings:
        return "warning"
    return "healthy"


def research_database_status(db: Session) -> dict:
    """Build the full read-only status payload for the dashboard panel."""
    settings = get_settings()
    hist = history_diagnostics(db)
    counts = hist.get("counts") or {}
    research_total = int(counts.get("research_market_snapshots") or 0)
    bounds = hist.get("research_bounds") or {}

    earliest = bounds.get("start_date")
    latest = bounds.get("end_date")

    last_import_candidates = [
        _safe(lambda: _iso(db.execute(select(func.max(ResearchMarketSnapshot.created_at))).scalar())),
        _safe(lambda: _iso(db.execute(select(func.max(HistoricalMarketSnapshot.created_at))).scalar())),
        hist.get("last_imported"),
    ]
    last_import = max((t for t in last_import_candidates if t), default=None)

    last_daily = _safe(
        lambda: bounds.get("end_date")
        or _iso(db.execute(select(func.max(ResearchMarketSnapshot.trade_date))).scalar()),
    )

    coverage = {}
    for key, meta in COVERAGE_FIELDS.items():
        cov = _field_coverage(db, meta["column"], meta["series"])
        coverage[key] = {"label": meta["label"], **cov}

    events = _event_counts(db)
    events_total = sum(v["count"] for v in events.values())

    progress = research_lab.evaluation_progress(db)
    learning_rows = _safe(
        lambda: int(db.execute(select(func.count(ResearchDailyLearning.id))).scalar() or 0),
        0,
    )

    calib = _safe(lambda: research_lab.calibration(db), {})
    cal_buckets = calib.get("buckets") or {}
    cal_ready = any((b.get("samples") or 0) >= 5 for b in cal_buckets.values())

    last_eval = _safe(
        lambda: _iso(db.execute(select(func.max(RecommendationOutcome.evaluated_at))).scalar()),
    )
    last_job = _safe(
        lambda: _iso(db.execute(select(func.max(JobRun.ran_at))).scalar()),
    )
    sched = _safe(lambda: job_status(db), {})

    cache_freshness = {}
    for key, label in (("usdmxn", "market"), ("news", "news"), ("calendar", "calendar")):
        entry = cache_manager.get(key)
        cache_freshness[label] = {
            "last_refresh": entry.fetched_at if entry else None,
            "source": entry.source if entry else None,
            "age_minutes": cache_manager.age_minutes(key),
        }

    provider_health = cache_manager.health_snapshot()
    api_errors = [
        {"provider": name, "status": rec.get("status"), "detail": rec.get("detail")}
        for name, rec in provider_health.items()
        if rec.get("status") in ("offline", "rate_limited", "using_fallback")
    ]

    data_class = hist.get("data_class", "sample")
    is_research = research_total > 0 and data_class != "sample"
    similarity_mode = hist.get("similarity_uses", "historical_event_reactions")

    if is_research:
        sim_status = "active"
        sim_label = f"Searching {research_total} daily environments"
    elif int(counts.get("historical_event_reactions") or 0) > 0:
        sim_status = "sample"
        sim_label = "Using sample event reactions"
    else:
        sim_status = "inactive"
        sim_label = "No historical data loaded"

    warnings: list[str] = list(hist.get("warnings") or [])
    errors: list[str] = []
    if not database_is_persistent():
        warnings.append("Storage is ephemeral — research data may not survive redeploys.")
    if research_total == 0:
        warnings.append("Research database empty — run: python -m app.scripts.backfill_history --importer research")
    for err in hist.get("errors") or []:
        errors.append(str(err))

    comparable_pool = research_total if is_research else int(counts.get("historical_event_reactions") or 0)
    avg_sample = min(25, comparable_pool) if comparable_pool else 0

    payload = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "database_version": RESEARCH_DATABASE_VERSION,
        "engine_versions": version_tags(),
        "historical_engine_version": HISTORICAL_ENGINE_VERSION,
        "active_importer": hist.get("active_importer"),
        "data_class": data_class,
        "storage": {
            "database_type": database_kind(),
            "persistent": database_is_persistent(),
        },
        "historical_research_database": {
            "market_days_stored": research_total,
            "earliest_date": earliest,
            "latest_date": latest,
            "total_historical_snapshots": int(counts.get("historical_market_snapshots") or 0),
            "total_research_snapshots": research_total,
            "database_version": RESEARCH_DATABASE_VERSION,
            "last_historical_import": last_import,
            "last_daily_update": last_daily,
        },
        "market_data_coverage": coverage,
        "economic_events": events,
        "research_statistics": {
            "historical_events_indexed": int(counts.get("historical_events") or 0),
            "economic_events_by_type": events_total,
            "comparable_setups_available": comparable_pool,
            "comparable_threshold": COMPARABLE_MIN_SIMILARITY,
            "average_comparable_sample_size": avg_sample,
            "similarity_engine_status": sim_status,
            "similarity_engine_label": sim_label,
            "similarity_mode": similarity_mode,
        },
        "ai_learning": {
            "recommendations_stored": progress.get("recommendations_stored", 0),
            "evaluations_completed": progress.get("recommendations_evaluated", 0),
            "pending_evaluations": progress.get("recommendations_pending", 0),
            "self_learning_rows": learning_rows,
            "self_learning_status": (
                "active" if learning_rows > 0
                else "schema_ready" if progress.get("recommendations_stored", 0) > 0
                else "awaiting_data"
            ),
            "self_learning_label": (
                f"{learning_rows} daily learning rows"
                if learning_rows > 0
                else "Collecting recommendations (self-learning schema ready)"
                if progress.get("recommendations_stored", 0) > 0
                else "Not yet active — schema ready"
            ),
            "calibration_status": "measured" if cal_ready else "insufficient_sample",
            "calibration_label": (
                "Calibration buckets have measured samples"
                if cal_ready
                else "Need more evaluated outcomes for calibration"
            ),
            "last_model_evaluation": last_eval,
        },
        "data_freshness": {
            "last_market_data_refresh": cache_freshness.get("market", {}).get("last_refresh"),
            "last_news_refresh": cache_freshness.get("news", {}).get("last_refresh"),
            "last_calendar_refresh": cache_freshness.get("calendar", {}).get("last_refresh"),
            "last_ai_recommendation": sched.get("last_recommendation_at") if sched else None,
            "last_background_update": last_job,
            "cache_detail": cache_freshness,
        },
        "system_health": {
            "historical_database": _health_level(warnings, errors),
            "historical_database_label": (
                "Healthy" if is_research else "Warning — sample or partial data"
            ),
            "provider_status": provider_health,
            "missing_data_warnings": [w for w in warnings if "empty" in w.lower() or "sample" in w.lower()],
            "warnings": warnings,
            "api_errors": api_errors,
            "errors": errors,
        },
        "importer": {
            "configured": settings.history_importer,
            "source_quality": get_importer(settings.history_importer).source_quality,
            "lookback_days": settings.history_lookback_days,
        },
    }
    return payload
