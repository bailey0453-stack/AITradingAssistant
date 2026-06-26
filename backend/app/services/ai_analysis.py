"""AI analysis engine for USD/MXN.

`get_analyzer()` returns a `RuleBasedAnalyzer` by default (uses `signals.py` and
composes a human-readable narrative). When an OpenAI key is configured and mock
mode is off, an LLM-backed analyzer can be returned instead. The output schema
is identical regardless of engine so routers/storage never change.

Analysis result schema:
    direction: "BUY_USD" | "SELL_USD" | "NO_TRADE"
    trade_score: float (0..100)
    market_bias: str
    confidence: float (0..100)
    momentum_status: str
    historical_similarity: dict (placeholder)
    risk_level: str
    summary: str
    key_drivers: list[str]
    entry: float | None
    target: float | None
    stretch_target: float | None
    stop: float | None
    expected_move: str
    expected_duration: str
    invalidation_level: float | None
    risk_notes: str
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import datetime, timezone

from app.config import Settings, get_settings
from app.services.market_data import MarketData
from app.services.signals import compute_signal

ANALYSIS_FIELDS = (
    "direction",
    "trade_score",
    "market_bias",
    "confidence",
    "momentum_status",
    "historical_similarity",
    "risk_level",
    "summary",
    "key_drivers",
    "entry",
    "target",
    "stretch_target",
    "stop",
    "expected_move",
    "expected_duration",
    "invalidation_level",
    "risk_notes",
)

_RISK_RANK = {"low": 0, "elevated": 1, "high": 2}


class AIAnalyzer(ABC):
    model_name = "base"

    @abstractmethod
    def analyze(
        self,
        market: MarketData,
        news: list[dict] | None = None,
        calendar: list[dict] | None = None,
        recent_analyses: list[dict] | None = None,
    ) -> dict:
        raise NotImplementedError


class RuleBasedAnalyzer(AIAnalyzer):
    """Transparent, deterministic analyzer driven by signal heuristics."""

    model_name = "mock-rules-v1"

    def analyze(
        self,
        market: MarketData,
        news: list[dict] | None = None,
        calendar: list[dict] | None = None,
        recent_analyses: list[dict] | None = None,
    ) -> dict:
        signal = compute_signal(market, news)
        direction = signal["direction"]

        risk_level, event_note = self._risk_with_events(signal["risk_level"], calendar)

        return {
            "direction": direction,
            "trade_score": signal["trade_score"],
            "market_bias": signal["market_bias"],
            "confidence": signal["confidence"],
            "momentum_status": signal["momentum_status"],
            "historical_similarity": self._historical_similarity(recent_analyses),
            "risk_level": risk_level,
            "summary": self._build_summary(market, signal),
            "key_drivers": signal["key_drivers"],
            "entry": signal["entry"],
            "target": signal["target"],
            "stretch_target": signal["stretch_target"],
            "stop": signal["stop"],
            "expected_move": signal["expected_move"],
            "expected_duration": self._expected_duration(signal),
            "invalidation_level": signal["invalidation_level"],
            "risk_notes": self._build_risk_notes(market, signal, event_note),
            "model": self.model_name,
        }

    @staticmethod
    def _upcoming_high_impact(calendar: list[dict] | None) -> list[dict]:
        if not calendar:
            return []
        now = datetime.now(timezone.utc)
        soon = []
        for ev in calendar:
            if ev.get("status") != "upcoming" or ev.get("importance") != "high":
                continue
            rt = ev.get("release_time")
            try:
                when = datetime.fromisoformat(rt) if rt else None
            except ValueError:
                when = None
            if when is not None and (when - now).total_seconds() <= 48 * 3600:
                soon.append(ev)
        return soon

    def _risk_with_events(
        self, base_risk: str, calendar: list[dict] | None
    ) -> tuple[str, str]:
        soon = self._upcoming_high_impact(calendar)
        if not soon:
            return base_risk, ""
        # A high-impact event within 48h lifts risk to at least "elevated".
        bumped = base_risk if _RISK_RANK.get(base_risk, 0) >= 1 else "elevated"
        names = ", ".join(e.get("event", "?") for e in soon[:3])
        return bumped, f"High-impact event(s) within 48h: {names}."

    @staticmethod
    def _historical_similarity(recent_analyses: list[dict] | None) -> dict:
        sample = len(recent_analyses or [])
        return {
            "status": "placeholder",
            "score": None,
            "sample_size": sample,
            "note": (
                "Historical similarity scoring arrives in a later phase once "
                "enough analysis snapshots are stored for matching."
            ),
        }

    @staticmethod
    def _expected_duration(signal: dict) -> str:
        if signal["direction"] == "NO_TRADE":
            return "n/a"
        ts = signal.get("trade_score") or 0
        if ts >= 70:
            return "1-2 days"
        if ts >= 40:
            return "2-4 days"
        return "3-5 days"

    @staticmethod
    def _build_summary(market: MarketData, signal: dict) -> str:
        price = market.usdmxn
        direction = signal["direction"]
        if direction == "BUY_USD":
            bias = (
                f"Bias favors USD strength vs MXN. Spot ~{price}. "
                f"Look to accumulate USD toward {signal['target']}, "
                f"stretch {signal['stretch_target']}."
            )
        elif direction == "SELL_USD":
            bias = (
                f"Bias favors MXN strength (USD/MXN lower). Spot ~{price}. "
                f"Look to fade USD toward {signal['target']}, "
                f"stretch {signal['stretch_target']}."
            )
        else:
            bias = (
                f"No clear edge. Spot ~{price}. Drivers are mixed; "
                f"stay flat until a catalyst confirms direction."
            )
        return (
            f"{bias} Trade score {signal['trade_score']}/100, "
            f"confidence {signal['confidence']}/100."
        )

    @staticmethod
    def _build_risk_notes(market: MarketData, signal: dict, event_note: str) -> str:
        notes = [
            "Mocked data in use; not investment advice."
            if market.source != "live"
            else "Live USD/MXN in use; macro inputs are placeholders — verify before acting.",
            f"Risk level: {signal['risk_level']} (VIX {market.vix}).",
        ]
        if event_note:
            notes.append(event_note)
        if signal["direction"] != "NO_TRADE":
            notes.append(f"Invalidate at stop {signal['stop']}.")
        return " ".join(notes)


class OpenAIAnalyzer(AIAnalyzer):  # pragma: no cover - stub
    """LLM-backed analyzer. Builds on the rule-based signal as a guardrail.

    Not active until OPENAI_API_KEY is set and USE_MOCK_DATA is false.
    """

    model_name = "openai"

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.model_name = settings.ai_model

    def analyze(
        self,
        market: MarketData,
        news: list[dict] | None = None,
        calendar: list[dict] | None = None,
        recent_analyses: list[dict] | None = None,
    ) -> dict:
        raise NotImplementedError(
            "OpenAIAnalyzer is not implemented yet. Use the rule-based analyzer."
        )


def get_analyzer(settings: Settings | None = None) -> AIAnalyzer:
    settings = settings or get_settings()
    if settings.is_mock or not settings.openai_api_key:
        return RuleBasedAnalyzer()
    return OpenAIAnalyzer(settings)
