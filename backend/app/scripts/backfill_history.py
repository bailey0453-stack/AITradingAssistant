"""Manual historical backfill CLI.

Run a real (or sample) historical import into the Phase 4 tables. This is the
*only* place an expensive provider backfill runs — page loads never trigger it.

Usage
-----
    python -m app.scripts.backfill_history                 # uses HISTORY_IMPORTER
    python -m app.scripts.backfill_history --importer csv
    python -m app.scripts.backfill_history --importer research   # full research DB
    python -m app.scripts.backfill_history --importer yahoo      # USD/MXN + cross-asset daily
    python -m app.scripts.backfill_history --importer alphavantage --no-throttle
    python -m app.scripts.backfill_history --importer csv --reset   # wipe + reimport

Importers
---------
    mock          self-contained sample (no key, no network) — the default safety net
    csv           CSV_HISTORY_DIR/events.csv (+ paths.csv, series.csv) — real, no key
    research      Yahoo + FRED (+ optional Alpha Vantage) → research_market_snapshots
    yahoo         USD/MXN, DXY, S&P, VIX, gold, WTI daily (no key)
    fred          US yields, rates, CPI/PCE, Banxico proxy, VIX, oil (FRED_API_KEY)
    fred_events   FRED-derived economic events (FRED_API_KEY)
    alphavantage  USD/MXN + WTI + S&P supplement (ALPHA_VANTAGE_API_KEY)

Required keys are read from the environment; they are never printed or logged.
"""

from __future__ import annotations

import argparse
import json
import sys

from sqlalchemy import delete

from app.config import get_settings
from app.database import SessionLocal, init_db
from app.models import (
    HistoricalEvent,
    HistoricalEventReaction,
    HistoricalMarketSnapshot,
    ResearchMarketSnapshot,
    SimilarityMatch,
)
from app.services.history.historical_events import history_diagnostics
from app.services.history.importers import (
    AlphaVantageImporter,
    CompositeResearchImporter,
    FREDImporter,
    get_importer,
)


def _reset_history(db) -> dict:
    """Delete all historical rows (FK-safe order) for a clean reimport."""
    counts = {}
    for model in (SimilarityMatch, HistoricalEventReaction,
                  HistoricalMarketSnapshot, HistoricalEvent, ResearchMarketSnapshot):
        counts[model.__tablename__] = db.execute(delete(model)).rowcount or 0
    db.commit()
    return counts


def _build_importer(name: str, args):
    """Construct the importer, passing CLI options to the network importers."""
    settings = get_settings()
    name = (name or "mock").lower()
    lookback = args.lookback_days or getattr(settings, "history_lookback_days", 3650)
    if name in ("alphavantage",):
        return AlphaVantageImporter(
            settings, throttle=not args.no_throttle, outputsize=args.outputsize
        )
    if name == "fred":
        return FREDImporter(settings, lookback_days=lookback)
    if name in ("yahoo", "fred_events"):
        from app.services.history.importers import FREDEconomicEventsImporter, YahooFinanceImporter

        if name == "yahoo":
            return YahooFinanceImporter(settings, lookback_days=lookback)
        return FREDEconomicEventsImporter(settings, lookback_days=lookback)
    if name in ("research", "composite"):
        return CompositeResearchImporter(settings, lookback_days=lookback)
    return get_importer(name, settings)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Backfill historical intelligence data.")
    parser.add_argument(
        "--importer", default=None,
        help="mock | csv | alphavantage | fred | yahoo (default: HISTORY_IMPORTER).",
    )
    parser.add_argument(
        "--reset", action="store_true",
        help="Delete existing historical rows before importing (safe-guarded by this flag).",
    )
    parser.add_argument(
        "--no-throttle", action="store_true",
        help="Alpha Vantage only: skip the inter-request delay (use real keys carefully).",
    )
    parser.add_argument(
        "--outputsize", default="compact", choices=["compact", "full"],
        help="Alpha Vantage daily history size (default: compact ~100 points).",
    )
    parser.add_argument(
        "--lookback-days", type=int, default=None,
        help="Observation lookback window in days (default: HISTORY_LOOKBACK_DAYS or 3650).",
    )
    args = parser.parse_args(argv)

    importer_name = (args.importer or get_settings().history_importer or "mock").lower()

    init_db()
    db = SessionLocal()
    try:
        report: dict = {"importer": importer_name}
        if args.reset:
            report["reset"] = _reset_history(db)

        importer = _build_importer(importer_name, args)
        report["result"] = importer.run_all(db)
        report["diagnostics"] = history_diagnostics(db)
    finally:
        db.close()

    print(json.dumps(report, indent=2, default=str))
    # Non-zero exit if a real import was requested but produced nothing useful.
    result = report.get("result", {})
    produced = (
        result.get("events", 0)
        + result.get("series_points", 0)
        + result.get("research_snapshots", 0)
    )
    if importer_name not in ("mock",) and produced == 0:
        print(
            f"\nWARNING: importer '{importer_name}' produced no rows. "
            "Check API keys / CSV_HISTORY_DIR. Errors: "
            f"{result.get('errors')}",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
