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
    opportunity_grade: str          # A+ | A | B | C | D | PASS (PASS == NO_TRADE)
    opportunity_grade_detail: dict  # grade score + reasons + components
    # Phase 4.5 strategist narrative (confidence = how sure; grade = how attractive)
    executive_summary: str
    why_this_grade: str
    why_not_higher: str
    why_not_lower: str
    current_trade_view: str
    trader_action: str
    quote_guidance: list[str]       # Border Currency desk pricing guidance
    risk_watchlist: list[str]
    invalidation_triggers: list[str]
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
    # Phase 4.5 strategist narrative
    "executive_summary",
    "why_this_grade",
    "why_not_higher",
    "why_not_lower",
    "current_trade_view",
    "trader_action",
    "quote_guidance",
    "risk_watchlist",
    "invalidation_triggers",
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

        # Phase 4.5: professional FX-strategist narrative.
        strategist = self._strategist_narrative(
            market, signal, regime, grade, upcoming_risks, agree, disagree,
            bullish, bearish,
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
            # Phase 4.5 strategist narrative (also spread to top level by router).
            "executive_summary": strategist["executive_summary"],
            "why_this_grade": strategist["why_this_grade"],
            "why_not_higher": strategist["why_not_higher"],
            "why_not_lower": strategist["why_not_lower"],
            "current_trade_view": strategist["current_trade_view"],
            "trader_action": strategist["trader_action"],
            "quote_guidance": strategist["quote_guidance"],
            "risk_watchlist": strategist["risk_watchlist"],
            "invalidation_triggers": strategist["invalidation_triggers"],
            "strategist": strategist,
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
            # PASS is reserved for "no trade" so the grade never conflicts with a
            # BUY_USD / SELL_USD direction (see Phase 4.5 consistency rules).
            grade = "PASS"
        else:
            # A directional read floors at "D" — a real bias is never graded PASS.
            grade = "D"
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

    # Action guidance per grade. PASS = no trade; D/C = bias-only, low quality;
    # B/A/A+ = increasingly actionable.
    _ACTION_BY_GRADE = {
        "PASS": (
            "Do not initiate a trade — no directional edge. Stand aside until a "
            "catalyst confirms direction."
        ),
        "D": (
            "Do not initiate a speculative trade — a directional lean exists but the "
            "setup is too weak; only act on operational need."
        ),
        "C": (
            "Low-quality setup — only trade if an operational need exists; otherwise "
            "wait. Keep size small and opportunistic."
        ),
        "B": "Tradeable — scale into the {dir} view on confirmation; size moderately.",
        "A": "Actionable — take the {dir} position; manage against the stop.",
        "A+": "High-conviction — lead with the {dir} view; press on confirmation.",
    }

    @classmethod
    def _strategist_narrative(
        cls,
        market: MarketData,
        signal: dict,
        regime: dict,
        grade: dict,
        upcoming_risks: list[dict] | None,
        agree: list[str] | None,
        disagree: list[str] | None,
        bullish: list[str] | None,
        bearish: list[str] | None,
    ) -> dict:
        """Compose a professional FX-strategist brief.

        Keeps two concepts distinct: ``confidence`` (how sure the system is in the
        read) vs ``opportunity_grade`` (how attractive the trade is right now).
        """
        direction = signal.get("direction")
        price = market.usdmxn
        letter = grade.get("grade")
        score = grade.get("score")
        conf = signal.get("confidence")
        ts = signal.get("trade_score")
        regime_name = (regime or {}).get("primary") or "mixed"
        agree = agree or []
        disagree = disagree or []

        dir_label = {
            "BUY_USD": "long-USD (USD/MXN higher)",
            "SELL_USD": "short-USD / long-MXN (USD/MXN lower)",
            "NO_TRADE": "neutral",
        }.get(direction, "neutral")
        dir_word = {"BUY_USD": "long-USD", "SELL_USD": "short-USD"}.get(direction, "directional")

        # --- Executive summary -------------------------------------------------
        if direction == "NO_TRADE":
            executive_summary = (
                f"No actionable USD/MXN edge right now (grade PASS, confidence "
                f"{conf}/100). Spot ~{_fmt(price)} in a {regime_name} regime with "
                f"mixed drivers — stand aside until a catalyst confirms direction."
            )
        else:
            lead = agree[0] if agree else (
                (bullish or bearish or ["mixed drivers"])[0]
            )
            executive_summary = (
                f"{letter}-grade {dir_label} setup on USD/MXN near {_fmt(price)} in a "
                f"{regime_name} regime. Confidence {conf}/100, trade score {ts}/100. "
                f"Lead driver: {lead}."
            )

        # --- Why this grade (separates grade vs confidence) --------------------
        # Drop the grade's own "Confidence X/100" reason so only the headline
        # (possibly blended) confidence number appears in the narrative.
        grade_reasons = [
            r for r in (grade.get("reasons") or [])
            if not str(r).startswith("Confidence ")
        ]
        why_this_grade = (
            f"Grade {letter} ({score}/100) measures how attractive the trade is now; "
            f"confidence {conf}/100 measures how sure the system is in the read "
            f"(trade score {ts}/100). " + " ".join(grade_reasons)
        )

        # --- Why not higher / lower -------------------------------------------
        caps: list[str] = []
        n_conf = len(signal.get("conflicting_signals") or [])
        if n_conf:
            caps.append(f"{n_conf} conflicting signal(s)")
        if disagree:
            caps.append(f"indicators pushing back ({', '.join(disagree)})")
        if signal.get("risk_level") in {"elevated", "high"}:
            caps.append(f"{signal.get('risk_level')} risk")
        vix = market.vix
        if vix is not None and vix > 18:
            caps.append(f"elevated volatility (VIX {_fmt(vix)})")
        if regime_name in _UNCERTAIN_REGIMES:
            caps.append(f"an uncertain {regime_name} regime")
        if (conf or 0) < 45:
            caps.append("modest conviction")
        if letter == "A+":
            why_not_higher = "Already the top grade; nothing material is capping it."
        elif caps:
            why_not_higher = "Held back by " + "; ".join(caps) + "."
        else:
            why_not_higher = (
                "Mainly the absence of a stronger, fully-aligned catalyst — the edge "
                "is real but not yet decisive."
            )

        supports: list[str] = []
        if agree:
            supports.append(f"agreeing indicators ({', '.join(agree)})")
        if (ts or 0) >= 50:
            supports.append(f"a solid trade score ({ts}/100)")
        if (regime or {}).get("confidence") and regime["confidence"] >= 50:
            supports.append(f"a fairly clear {regime_name} regime ({regime['confidence']}% conf)")
        lead_factors = (bullish if direction == "BUY_USD" else bearish) or []
        if lead_factors:
            supports.append(f"supportive drivers ({lead_factors[0]})")
        if direction == "NO_TRADE":
            why_not_lower = (
                "There is a mild lean in places, but not enough agreement to justify "
                "any active exposure."
            )
        elif supports:
            why_not_lower = "Supported by " + "; ".join(supports) + "."
        else:
            why_not_lower = (
                "A coherent directional bias keeps it above the floor even though "
                "conviction is light."
            )

        # --- Current trade view + action --------------------------------------
        if direction == "BUY_USD":
            current_trade_view = (
                f"Constructive on USD vs MXN. Spot ~{_fmt(price)}; targeting "
                f"{_fmt(signal.get('target'))} (stretch {_fmt(signal.get('stretch_target'))}), "
                f"stop {_fmt(signal.get('stop'))}."
            )
        elif direction == "SELL_USD":
            current_trade_view = (
                f"Constructive on MXN (USD/MXN lower). Spot ~{_fmt(price)}; targeting "
                f"{_fmt(signal.get('target'))} (stretch {_fmt(signal.get('stretch_target'))}), "
                f"stop {_fmt(signal.get('stop'))}."
            )
        else:
            current_trade_view = (
                f"Neutral / flat. Spot ~{_fmt(price)}; no directional conviction — "
                f"drivers are mixed."
            )
        trader_action = cls._ACTION_BY_GRADE.get(letter, "Monitor.").format(dir=dir_word)

        # --- Quote guidance for Border Currency operations --------------------
        quote_guidance = cls._quote_guidance(
            market, signal, regime, grade, upcoming_risks
        )

        # --- Risk watchlist ----------------------------------------------------
        risk_watchlist: list[str] = []
        for ev in (upcoming_risks or [])[:3]:
            when = f"~{ev['hours_away']}h" if ev.get("hours_away") is not None else "soon"
            risk_watchlist.append(
                f"{ev.get('event')} ({ev.get('importance')} impact, {when})"
            )
        for c in (signal.get("conflicting_signals") or [])[:2]:
            risk_watchlist.append(f"Conflict: {c.get('label')} ({c.get('detail')})")
        if vix is not None and vix > 20:
            risk_watchlist.append(f"Elevated volatility (VIX {_fmt(vix)})")
        if regime_name in _UNCERTAIN_REGIMES:
            risk_watchlist.append(f"Headline-driven {regime_name} regime")
        if not risk_watchlist:
            risk_watchlist.append("No major scheduled catalysts in the immediate window.")

        # --- Invalidation triggers --------------------------------------------
        invalidation_triggers: list[str] = []
        if direction != "NO_TRADE" and signal.get("stop") is not None:
            side = "above" if direction == "SELL_USD" else "below"
            invalidation_triggers.append(
                f"USD/MXN trades {side} the stop at {_fmt(signal['stop'])}."
            )
        if direction != "NO_TRADE" and disagree:
            invalidation_triggers.append(
                f"Pushback indicators ({', '.join(disagree)}) take over the tape."
            )
        for ev in (upcoming_risks or []):
            if ev.get("importance") == "high":
                when = f"~{ev['hours_away']}h" if ev.get("hours_away") is not None else "soon"
                invalidation_triggers.append(
                    f"A surprise in {ev.get('event')} ({when}) resets the bias."
                )
                break
        if regime_name and regime_name != "mixed":
            invalidation_triggers.append(
                f"A shift out of the {regime_name} regime changes the playbook."
            )
        if direction == "NO_TRADE":
            invalidation_triggers.append(
                "A decisive, agreeing move in DXY/yields would create a directional edge."
            )

        return {
            "executive_summary": executive_summary,
            "why_this_grade": why_this_grade,
            "why_not_higher": why_not_higher,
            "why_not_lower": why_not_lower,
            "current_trade_view": current_trade_view,
            "trader_action": trader_action,
            "quote_guidance": quote_guidance,
            "risk_watchlist": risk_watchlist[:6],
            "invalidation_triggers": invalidation_triggers[:6],
        }

    @classmethod
    def strategist_from_result(
        cls,
        result: dict,
        market: MarketData,
        confidence_override: float | None = None,
    ) -> dict:
        """Rebuild the strategist brief from an analysis ``result``.

        Used by the analysis endpoint to refresh the narrative *after* Phase 4
        blends historical/regime/volatility into a final ``confidence``, so the
        brief and the top-level ``confidence`` field always agree.
        """
        direction = result.get("direction")
        conf = (
            confidence_override
            if confidence_override is not None
            else result.get("confidence")
        )
        signal_like = {
            "direction": direction,
            "confidence": conf,
            "trade_score": result.get("trade_score"),
            "conflicting_signals": result.get("conflicting_signals"),
            "risk_level": result.get("risk_level"),
            "target": result.get("target"),
            "stretch_target": result.get("stretch_target"),
            "stop": result.get("stop"),
        }
        agree, disagree = cls._indicator_agreement(
            result.get("market_drivers") or [], direction
        )
        return cls._strategist_narrative(
            market,
            signal_like,
            result.get("market_regime") or {},
            result.get("opportunity_grade_detail") or {},
            result.get("upcoming_risks") or [],
            agree,
            disagree,
            result.get("bullish_factors") or [],
            result.get("bearish_factors") or [],
        )

    @staticmethod
    def _quote_guidance(
        market: MarketData,
        signal: dict,
        regime: dict,
        grade: dict,
        upcoming_risks: list[dict] | None,
    ) -> list[str]:
        """Operational pricing guidance for Border Currency desk operators."""
        out: list[str] = []
        price = market.usdmxn
        vix = market.vix if market.vix is not None else 15.0
        risk = signal.get("risk_level")
        regime_name = (regime or {}).get("primary")
        direction = signal.get("direction")

        # Imminent high-impact event (within 24h)?
        soon_event = None
        for ev in upcoming_risks or []:
            if ev.get("importance") == "high":
                h = ev.get("hours_away")
                if h is None or h <= 24:
                    soon_event = ev
                    break

        volatile = vix > 20 or regime_name in {"High Volatility", "Trade War", "Risk Off"}

        if soon_event:
            when = (
                f"~{soon_event['hours_away']}h"
                if soon_event.get("hours_away") is not None
                else "soon"
            )
            out.append(
                f"Avoid aggressive pricing before high-impact event "
                f"({soon_event.get('event')}, {when})."
            )
            out.append("Keep quote validity short until the event clears.")
        if volatile:
            out.append("Widen spread slightly to account for elevated volatility.")
        if price:
            threshold = round(price * (0.005 if volatile else 0.003), 4)
            out.append(
                f"Requote if USD/MXN moves beyond ±{threshold} from {_fmt(price)}."
            )
        if direction != "NO_TRADE" and grade.get("grade") in {"A", "A+", "B"}:
            lean = "USD strength" if direction == "BUY_USD" else "peso strength"
            out.append(
                f"Bias favors {lean}; pricing can lean accordingly but avoid overcommitting."
            )
        if not soon_event and not volatile:
            out.insert(0, "Quote normally; conditions are orderly.")

        # De-dupe, preserve order.
        seen: set[str] = set()
        deduped = []
        for line in out:
            if line.lower() not in seen:
                seen.add(line.lower())
                deduped.append(line)
        return deduped

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
