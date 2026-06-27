"""Component version tags stamped onto every stored recommendation.

These let the AI Research Lab compare model performance across versions over
time (e.g. "model 0.6 outperforms 0.5"). Bump a value when the corresponding
engine's behavior changes so historical recommendations remain attributable.
"""

from __future__ import annotations

from app import __version__

# Overall app/model release (mirrors app.__version__).
MODEL_VERSION = __version__
# Explainable reasoning engine (Phase 3.5 lineage).
REASONING_ENGINE_VERSION = "3.5"
# Named signal-weighting profile (see services/signal_weights.py).
WEIGHTING_PROFILE = "default-v1"
# Historical / evidence engine (Phase 5 lineage).
HISTORICAL_ENGINE_VERSION = "5.0"


def version_tags() -> dict:
    return {
        "model_version": MODEL_VERSION,
        "reasoning_engine_version": REASONING_ENGINE_VERSION,
        "weighting_profile": WEIGHTING_PROFILE,
        "historical_engine_version": HISTORICAL_ENGINE_VERSION,
    }
