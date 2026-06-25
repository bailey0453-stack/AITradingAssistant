"""AI analysis engine for USD/MXN.

`get_analyzer()` returns a `RuleBasedAnalyzer` by default (uses `signals.py` and
composes a human-readable narrative). When an OpenAI key is configured and mock
mode is off, an LLM-backed analyzer can be returned instead. The output schema
is identical regardless of engine so routers/storage never change.

Analysis result schema:
    direction: "BUY_USD" | "SELL_USD" | "NO_TRADE"
    confidence: float (0..100)
    summary: str
    key_drivers: list[str]
    target: float | None
    stretch_target: float | None
    stop: float | None
    momentum_status: str
    risk_notes: str
"""

from __future__ import annotations

from abc import ABC, abstractmethod

from app.config import Settings, get_settings
from app.services.market_data import MarketData
from app.services.signals import compute_signal

ANALYSIS_FIELDS = (
    "direction",
    "confidence",
    "summary",
    "key_drivers",
    "target",
    "stretch_target",
    "stop",
    "momentum_status",
    "risk_notes",
)


class AIAnalyzer(ABC):
    model_name = "base"

    @abstractmethod
    def analyze(self, market: MarketData, news: list[dict] | None = None) -> dict:
        raise NotImplementedError


class RuleBasedAnalyzer(AIAnalyzer):
    """Transparent, deterministic analyzer driven by signal heuristics."""

    model_name = "mock-rules-v1"

    def analyze(self, market: MarketData, news: list[dict] | None = None) -> dict:
        signal = compute_signal(market, news)
        direction = signal["direction"]

        summary = self._build_summary(market, signal)
        risk_notes = self._build_risk_notes(market, signal)

        return {
            "direction": direction,
            "confidence": signal["confidence"],
            "summary": summary,
            "key_drivers": signal["key_drivers"],
            "target": signal["target"],
            "stretch_target": signal["stretch_target"],
            "stop": signal["stop"],
            "momentum_status": signal["momentum_status"],
            "risk_notes": risk_notes,
            "model": self.model_name,
        }

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
        return f"{bias} Confidence {signal['confidence']}/100."

    @staticmethod
    def _build_risk_notes(market: MarketData, signal: dict) -> str:
        notes = [
            "Mocked data in use; not investment advice." if market.source == "mock"
            else "Live data in use; verify before acting.",
            "Event risk: US CPI and Banxico on the calendar can override technicals.",
        ]
        if signal["direction"] != "NO_TRADE":
            notes.append(f"Invalidate below/above stop {signal['stop']}.")
        return " ".join(notes)


class OpenAIAnalyzer(AIAnalyzer):  # pragma: no cover - stub
    """LLM-backed analyzer. Builds on the rule-based signal as a guardrail.

    Not active until OPENAI_API_KEY is set and USE_MOCK_DATA is false.
    """

    model_name = "openai"

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.model_name = settings.ai_model

    def analyze(self, market: MarketData, news: list[dict] | None = None) -> dict:
        raise NotImplementedError(
            "OpenAIAnalyzer is not implemented yet. Use the rule-based analyzer."
        )


def get_analyzer(settings: Settings | None = None) -> AIAnalyzer:
    settings = settings or get_settings()
    if settings.is_mock or not settings.openai_api_key:
        return RuleBasedAnalyzer()
    return OpenAIAnalyzer(settings)
