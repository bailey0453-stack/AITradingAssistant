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
    market_drivers: list[dict]      # per-indicator value + USD+/MXN+/neutral lean
    bullish_factors: list[str]      # what supports USD strength
    bearish_factors: list[str]      # what supports MXN strength / USD weakness
    conflicting_signals: list[dict] # signals pushing against the net bias
    upcoming_risks: list[dict]      # high/medium events ahead that could move it
    what_would_change_my_mind: list[str]  # concrete conditions that flip the view
    market_regime: dict             # primary/secondary regime + confidence
    opportunity_grade: str          # A+ | A | B | C | D | PASS
    opportunity_grade_detail: dict  # grade score + reasons + components
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
from app.services.market_regime import detect_regime
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
    "market_drivers",
    "bullish_factors",
    "bearish_factors",
    "upcoming_risks",
    "what_would_change_my_mind",
    "market_regime",
    "opportunity_grade",
    "opportunity_grade_detail",
    "entry",
    "target",
    "stretch_target",
    "stop",
    "expected_move",
    "expected_duration",
    "invalidation_level",
    "risk_notes",
    "weighted_contributions",
    "conflicting_signals",
    "signal_breakdown",
)

# Composite-score thresholds -> letter grade (highest first).
_GRADE_BANDS = (
    (85.0, "A+"),
    (74.0, "A"),
    (60.0, "B"),
    (46.0, "C"),
    (32.0, "D"),
)
# Regimes where conviction should be capped (uncertain / headline-driven tape).
_UNCERTAIN_REGIMES = {"High Volatility", "Political Risk", "Trade War", "Risk Off"}

_RISK_RANK = {"low": 0, "elevated": 1, "high": 2}


def _only_upcoming(calendar: list[dict] | None) -> list[dict]:
    return [e for e in (calendar or []) if e.get("status") == "upcoming"]


def _only_released(calendar: list[dict] | None) -> list[dict]:
    return [e for e in (calendar or []) if e.get("status") == "released"]


def _fmt(value) -> str:
    if value is None:
        return "—"
    if isinstance(value, float):
        return f"{value:g}"
    return str(value)


def _to_num(value) -> float | None:
    """Best-effort numeric parse of calendar values like '0.4%', '206K', '1.4'."""
    if value is None:
        return None
    text = str(value).strip().lower().replace(",", "").replace("%", "")
    mult = 1.0
    if text.endswith("k"):
        mult, text = 1e3, text[:-1]
    elif text.endswith("m"):
        mult, text = 1e6, text[:-1]
    elif text.endswith("b"):
        mult, text = 1e9, text[:-1]
    try:
        return float(text) * mult
    except ValueError:
        return None


def _event_surprise(event: dict) -> str | None:
    """Compare actual vs forecast to lean USD+ / MXN+ (or None if unknown).

    Higher-than-forecast US data -> USD+. Higher-than-forecast Mexico activity
    data is treated as MXN+ (growth-positive). Returns None when not computable.
    """
    actual = _to_num(event.get("actual"))
    forecast = _to_num(event.get("forecast"))
    if actual is None or forecast is None or actual == forecast:
        return None
    beat = actual > forecast
    impact = (event.get("currency_impact") or "USD").upper()
    if impact == "USD":
        return "USD+" if beat else "MXN+"
    # Mexico activity beat -> peso-positive.
    return "MXN+" if beat else "USD+"


class AIAnalyzer(ABC):
    model_name = "base"

    @abstractmethod
    def analyze(
        self,
        market: MarketData,
        news: list[dict] | None = None,
        calendar: list[dict] | None = None,
        recent_analyses: list[dict] | None = None,
        context: dict | None = None,
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
        context: dict | None = None,
    ) -> dict:
        context = context or {}
        upcoming = context.get("upcoming_events") or _only_upcoming(calendar)
        released_24h = context.get("released_last_24h") or _only_released(calendar)

        # The weighting engine scores the evidence; weights are configurable.
        signal = compute_signal(
            market,
            news=news,
            released_events=released_24h,
            momentum=context.get("momentum"),
        )
        direction = signal["direction"]

        risk_level, event_note = self._risk_with_events(signal["risk_level"], calendar)

        market_drivers = self._market_drivers(market)
        bullish, bearish = self._directional_factors(
            market_drivers, news, released_24h
        )
        agree, disagree = self._indicator_agreement(market_drivers, direction)
        upcoming_risks = self._upcoming_risks(upcoming)

        # Phase 3.5 reasoning layer: regime, grade, and what would flip the view.
        regime = detect_regime(
            market,
            news=news,
            calendar=calendar,
            momentum=context.get("momentum"),
            signal=signal,
        )
        grade = self._opportunity_grade(signal, regime, market)
        wwcm = self._what_would_change_my_mind(
            market, signal, regime, upcoming_risks
        )

        return {
            "direction": direction,
            "trade_score": signal["trade_score"],
            "market_bias": signal["market_bias"],
            "confidence": signal["confidence"],
            "momentum_status": signal["momentum_status"],
            "historical_similarity": self._historical_similarity(recent_analyses),
            "risk_level": risk_level,
            "summary": self._build_summary(market, signal, agree, disagree),
            "key_drivers": signal["key_drivers"],
            "market_drivers": market_drivers,
            "bullish_factors": bullish,
            "bearish_factors": bearish,
            "upcoming_risks": upcoming_risks,
            "what_would_change_my_mind": wwcm,
            "market_regime": regime,
            "opportunity_grade": grade["grade"],
            "opportunity_grade_detail": grade,
            "entry": signal["entry"],
            "target": signal["target"],
            "stretch_target": signal["stretch_target"],
            "stop": signal["stop"],
            "expected_move": signal["expected_move"],
            "expected_duration": self._expected_duration(signal),
            "invalidation_level": signal["invalidation_level"],
            "risk_notes": self._build_risk_notes(market, signal, event_note),
            "weighted_contributions": signal["weighted_contributions"],
            "conflicting_signals": signal["conflicting_signals"],
            "signal_breakdown": signal["signal_breakdown"],
            "model": self.model_name,
        }

    @staticmethod
    def _market_drivers(market: MarketData) -> list[dict]:
        """Per-indicator read: value + which currency it currently favors.

        ``lean`` is one of "USD+" (favors USD strength / USD/MXN up), "MXN+"
        (favors peso strength / USD/MXN down), or "neutral".
        """
        d = market.drivers or {}

        def lean(delta: float, positive_is_usd: bool, tol: float) -> str:
            if abs(delta) <= tol:
                return "neutral"
            usd_favored = (delta > 0) == positive_is_usd
            return "USD+" if usd_favored else "MXN+"

        drivers = [
            {
                "name": "DXY",
                "value": market.dxy,
                "lean": lean(float(d.get("dxy_delta", 0.0)), True, 0.05),
                "note": "US dollar index vs recent baseline",
            },
            {
                "name": "US 10Y yield",
                "value": market.us10y,
                "lean": lean(float(d.get("yield_delta", 0.0)), True, 0.01),
                "note": "Higher yields tend to support USD",
            },
            {
                "name": "US 2Y yield",
                "value": market.us2y,
                "lean": lean(float(d.get("us2y_delta", 0.0)), True, 0.01),
                "note": "Front-end rates / Fed expectations",
            },
            {
                "name": "WTI oil",
                "value": market.oil,
                # Higher oil is a tailwind for the peso (MXN+).
                "lean": lean(float(d.get("oil_delta", 0.0)), False, 0.3),
                "note": "Higher oil supports MXN (Mexico is an exporter)",
            },
            {
                "name": "VIX",
                "value": market.vix,
                # Risk-off (higher VIX) supports the safe-haven USD.
                "lean": lean(float(d.get("vix_delta", 0.0)), True, 0.5),
                "note": "Higher volatility = risk-off = USD haven bid",
            },
            {
                "name": "S&P futures",
                "value": market.sp_futures,
                # Risk-on (higher equities) supports the peso.
                "lean": lean(float(d.get("sp_delta", 0.0)), False, 10.0),
                "note": "Risk-on equities support EM / MXN",
            },
        ]
        return [d for d in drivers if d["value"] is not None]

    @staticmethod
    def _directional_factors(
        market_drivers: list[dict],
        news: list[dict] | None,
        released_24h: list[dict] | None,
    ) -> tuple[list[str], list[str]]:
        bullish: list[str] = []  # supports USD strength
        bearish: list[str] = []  # supports MXN strength / USD weakness
        seen_bull: set[str] = set()
        seen_bear: set[str] = set()

        def add(target: list[str], seen: set[str], line: str) -> None:
            key = line.lower()
            if key not in seen:
                seen.add(key)
                target.append(line)

        # Market drivers: describe the *current* lean (not the static relationship).
        for drv in market_drivers:
            line = f"{drv['name']} {_fmt(drv['value'])} → supports "
            if drv["lean"] == "USD+":
                add(bullish, seen_bull, line + "USD")
            elif drv["lean"] == "MXN+":
                add(bearish, seen_bear, line + "MXN")

        # News (sentiment is a placeholder lean), de-duplicated by headline.
        for item in news or []:
            sentiment = str(item.get("sentiment", "neutral")).lower()
            headline = (item.get("headline") or "").strip()
            if not headline:
                continue
            if sentiment == "usd_bullish":
                add(bullish, seen_bull, f"News: {headline}")
            elif sentiment == "mxn_bullish":
                add(bearish, seen_bear, f"News: {headline}")

        # Recently released data, where actual vs forecast is computable.
        for ev in released_24h or []:
            surprise = _event_surprise(ev)
            name = ev.get("event")
            if surprise == "USD+":
                add(bullish, seen_bull, f"Data: {name} beat forecast (USD-positive)")
            elif surprise == "MXN+":
                add(bearish, seen_bear, f"Data: {name} (MXN-positive)")

        return bullish, bearish

    @staticmethod
    def _indicator_agreement(
        market_drivers: list[dict], direction: str
    ) -> tuple[list[str], list[str]]:
        """Which indicators agree vs disagree with the chosen direction."""
        if direction == "NO_TRADE":
            return [], []
        favored = "USD+" if direction == "BUY_USD" else "MXN+"
        opposed = "MXN+" if direction == "BUY_USD" else "USD+"
        agree = [d["name"] for d in market_drivers if d["lean"] == favored]
        disagree = [d["name"] for d in market_drivers if d["lean"] == opposed]
        return agree, disagree

    @staticmethod
    def _opportunity_grade(signal: dict, regime: dict, market: MarketData) -> dict:
        """Grade the setup A+..PASS from agreement, regime, risk, conf & volatility.

        Composite score (0..100) blends conviction and confidence, then deducts
        penalties for risk, conflicting signals and high volatility. Uncertain
        regimes cap the top grade. NO_TRADE always grades PASS.
        """
        sb = signal.get("signal_breakdown") or {}
        total = float(sb.get("total_score") or 0.0)
        net = abs(float(sb.get("net_score") or 0.0))
        agreement = (net / total) if total else 0.0           # 0..1 conviction
        confidence = float(signal.get("confidence") or 0.0) / 100.0
        trade = float(signal.get("trade_score") or 0.0) / 100.0

        risk_penalty = {"low": 0.0, "elevated": 8.0, "high": 18.0}.get(
            signal.get("risk_level"), 8.0
        )
        n_conflicts = len(signal.get("conflicting_signals") or [])
        conflict_penalty = min(20.0, n_conflicts * 5.0)
        vix = market.vix if market.vix is not None else 15.0
        vol_penalty = max(0.0, (vix - 18.0)) * 1.5            # historical-vol proxy

        base = 100.0 * (0.4 * trade + 0.3 * agreement + 0.3 * confidence)
        score = round(base - risk_penalty - conflict_penalty - vol_penalty, 1)

        direction = signal.get("direction")
        if direction == "NO_TRADE":
            grade = "PASS"
        else:
            grade = "PASS"
            for threshold, letter in _GRADE_BANDS:
                if score >= threshold:
                    grade = letter
                    break
            # Don't hand out an A+ when the regime itself is uncertain.
            if grade == "A+" and regime.get("primary") in _UNCERTAIN_REGIMES:
                grade = "A"

        reasons: list[str] = []
        reasons.append(
            f"Signal agreement {round(agreement * 100)}% "
            f"(net {sb.get('net_score')} of {sb.get('total_score')} total weight)."
        )
        reasons.append(
            f"Confidence {signal.get('confidence')}/100, trade score "
            f"{signal.get('trade_score')}/100."
        )
        reasons.append(
            f"Regime: {regime.get('primary')} "
            f"({regime.get('confidence')}% conf)."
        )
        if risk_penalty:
            reasons.append(f"Risk {signal.get('risk_level')} (-{risk_penalty:g}).")
        if conflict_penalty:
            reasons.append(
                f"{n_conflicts} conflicting signal(s) (-{conflict_penalty:g})."
            )
        if vol_penalty:
            reasons.append(f"Elevated volatility VIX {vix} (-{round(vol_penalty, 1)}).")
        if direction == "NO_TRADE":
            reasons.append("No directional edge -> PASS.")

        return {
            "grade": grade,
            "score": score,
            "reasons": reasons,
            "components": {
                "agreement": round(agreement, 3),
                "confidence": round(confidence, 3),
                "trade_score": round(trade, 3),
                "regime_confidence": regime.get("confidence"),
                "risk_penalty": risk_penalty,
                "conflict_penalty": conflict_penalty,
                "volatility_penalty": round(vol_penalty, 2),
            },
        }

    @staticmethod
    def _what_would_change_my_mind(
        market: MarketData,
        signal: dict,
        regime: dict,
        upcoming_risks: list[dict] | None,
    ) -> list[str]:
        """Concrete, falsifiable conditions that would weaken or flip the view."""
        out: list[str] = []
        direction = signal.get("direction")

        if direction != "NO_TRADE" and signal.get("stop") is not None:
            side = "above" if direction == "SELL_USD" else "below"
            out.append(
                f"USD/MXN trading {side} the stop at {signal['stop']} "
                f"would invalidate the {direction} view."
            )

        # Top opposing signals: if they strengthen, the net bias erodes.
        for c in (signal.get("conflicting_signals") or [])[:2]:
            out.append(
                f"If {c.get('label')} strengthens further ({c.get('detail')}), "
                f"the net bias weakens or flips."
            )

        # Imminent high-impact catalysts.
        for ev in upcoming_risks or []:
            if ev.get("importance") == "high":
                when = (
                    f"~{ev['hours_away']}h"
                    if ev.get("hours_away") is not None
                    else "soon"
                )
                out.append(
                    f"A surprise in {ev.get('event')} ({when}) could reset the bias."
                )
                break

        if direction == "NO_TRADE":
            out.append(
                "A decisive, agreeing move in DXY/yields or a high-impact data "
                "surprise would create a directional edge."
            )

        # A regime shift changes the whole playbook.
        if regime.get("primary"):
            out.append(
                f"A shift out of the '{regime['primary']}' regime would change "
                f"how these signals should be traded."
            )

        # De-dupe while preserving order, cap the list.
        seen: set[str] = set()
        deduped = []
        for line in out:
            if line.lower() not in seen:
                seen.add(line.lower())
                deduped.append(line)
        return deduped[:6]

    @staticmethod
    def _upcoming_risks(upcoming: list[dict] | None) -> list[dict]:
        """High/medium-impact events ahead that could move or invalidate a view."""
        now = datetime.now(timezone.utc)
        risks: list[dict] = []
        for ev in upcoming or []:
            if ev.get("importance") not in {"high", "medium"}:
                continue
            rt = ev.get("release_time")
            hours = None
            try:
                when = datetime.fromisoformat(str(rt).replace("Z", "+00:00")) if rt else None
                if when is not None:
                    if when.tzinfo is None:
                        when = when.replace(tzinfo=timezone.utc)
                    hours = round((when - now).total_seconds() / 3600.0, 1)
            except ValueError:
                when = None
            risks.append(
                {
                    "event": ev.get("event"),
                    "country": ev.get("country"),
                    "importance": ev.get("importance"),
                    "release_time": rt,
                    "hours_away": hours,
                    "note": (
                        "Could trigger volatility / invalidate the view"
                        if ev.get("importance") == "high"
                        else "Secondary catalyst to watch"
                    ),
                }
            )
            if len(risks) >= 6:
                break
        return risks

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
    def _build_summary(
        market: MarketData,
        signal: dict,
        agree: list[str] | None = None,
        disagree: list[str] | None = None,
    ) -> str:
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

        # The "why": which indicators line up, and which push the other way.
        why = ""
        if direction != "NO_TRADE":
            if agree:
                why += f" Confirming: {', '.join(agree)}."
            if disagree:
                why += f" Pushing back: {', '.join(disagree)}."

        return (
            f"{bias}{why} Trade score {signal['trade_score']}/100, "
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
        context: dict | None = None,
    ) -> dict:
        raise NotImplementedError(
            "OpenAIAnalyzer is not implemented yet. Use the rule-based analyzer."
        )


def get_analyzer(settings: Settings | None = None) -> AIAnalyzer:
    settings = settings or get_settings()
    if settings.is_mock or not settings.openai_api_key:
        return RuleBasedAnalyzer()
    return OpenAIAnalyzer(settings)
