"""Manual historical backfill CLI.

Run a real (or sample) historical import into the Phase 4 tables. This is the
*only* place an expensive provider backfill runs — page loads never trigger it.

Usage
-----
    python -m app.scripts.backfill_history                 # uses HISTORY_IMPORTER
    python -m app.scripts.backfill_history --importer csv
    python -m app.scripts.backfill_history --importer fred
    python -m app.scripts.backfill_history --importer alphavantage --no-throttle
    python -m app.scripts.backfill_history --importer csv --reset   # wipe + reimport

Importers
---------
    mock          self-contained sample (no key, no network) — the default safety net
    csv           CSV_HISTORY_DIR/events.csv (+ paths.csv, series.csv) — real, no key
    alphavantage  USD/MXN + WTI oil + S&P proxy daily history (ALPHA_VANTAGE_API_KEY)
    fred          US 2Y/10Y yields, dollar-index proxy, VIX, WTI (FRED_API_KEY)
    yahoo         not implemented (stub)

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
    SimilarityMatch,
)
from app.services.history.historical_events import history_diagnostics
from app.services.history.importers import (
    AlphaVantageImporter,
    FREDImporter,
    get_importer,
)


def _reset_history(db) -> dict:
    """Delete all historical rows (FK-safe order) for a clean reimport."""
    counts = {}
    for model in (SimilarityMatch, HistoricalEventReaction,
                  HistoricalMarketSnapshot, HistoricalEvent):
        counts[model.__tablename__] = db.execute(delete(model)).rowcount or 0
    db.commit()
    return counts


def _build_importer(name: str, args):
    """Construct the importer, passing CLI options to the network importers."""
    settings = get_settings()
    name = (name or "mock").lower()
    if name == "alphavantage":
        return AlphaVantageImporter(
            settings, throttle=not args.no_throttle, outputsize=args.outputsize
        )
    if name == "fred":
        return FREDImporter(settings, lookback_days=args.lookback_days)
    return get_importer(name)


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
        "--lookback-days", type=int, default=1825,
        help="FRED observation lookback window in days (default: 1825 ≈ 5y).",
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
    produced = (result.get("events", 0) + result.get("series_points", 0))
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
