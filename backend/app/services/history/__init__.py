"""Historical intelligence engine (Phase 4).

Answers: *"Have we seen conditions like this before, and what usually happened
next?"* by backfilling historical market data + economic events, measuring how
USD/MXN reacted in fixed windows, and ranking past events by similarity to the
current context.

Modules
-------
- ``importers``             — modular import framework (CSV / Yahoo / FRED /
  Alpha Vantage / Polygon stubs + a working mock/sample importer).
- ``historical_prices``     — reaction-window math over a price path.
- ``historical_events``     — read/aggregate events + reactions from the DB.
- ``similarity_engine``     — feature vectors + similarity scoring + ranking.
- ``historical_statistics`` — aggregate stats, probability forecast, confidence
  blending.

Everything degrades to mock/sample data and never requires a paid provider.
"""

from app.services.history.historical_events import (
    ensure_history_seeded,
    list_events,
    load_reactions,
)
from app.services.history.historical_statistics import (
    aggregate_statistics,
    blend_confidence,
    probability_forecast,
)
from app.services.history.similarity_engine import (
    build_feature_vector,
    find_similar,
    persist_matches,
)

__all__ = [
    "ensure_history_seeded",
    "list_events",
    "load_reactions",
    "aggregate_statistics",
    "blend_confidence",
    "probability_forecast",
    "build_feature_vector",
    "find_similar",
    "persist_matches",
]
