# Roadmap

## Phase 1 — Backend intelligence service (current)

Goal: a working, backend-only USD/MXN intelligence API on mocked data.

- [x] FastAPI app with `/health`, `/market/usdmxn`, `/analysis/usdmxn`.
- [x] Persist market snapshots and AI analysis snapshots (SQLite, Postgres-ready).
- [x] Track USD/MXN + DXY, treasury yield, oil, news, economic calendar (placeholders).
- [x] Modular provider layer (mock now, live later).
- [x] Rule-based analysis engine returning direction, confidence, summary, key
      drivers, target, stretch target, stop, momentum status, risk notes.
- [x] Optional HTML dashboard.

## Phase 2 — Market intelligence engine (current)

Goal: turn the price watcher into an intelligence engine with news, calendar,
context, richer analysis, a timeline, and a stored recommendation dataset.

- [x] Live USD/MXN provider (Open Exchange Rates) with mock fallback + source tagging.
- [x] Expanded market snapshot: USD/MXN, inverse, DXY, US 2Y, US 10Y, WTI, gold,
      S&P futures, VIX, provider, source, timestamp.
- [x] Modular news provider + `NewsItem` model; news stored (deduplicated).
- [x] Economic calendar service (`calendar.py`) covering US + Mexico events.
- [x] Context builder assembling market + news + events + recent analyses.
- [x] Richer analysis: trade_score, market_bias, risk_level, momentum,
      historical_similarity (placeholder), entry/target/stretch/stop,
      expected_move/duration, invalidation_level, risk_notes.
- [x] Event timeline (released events, market moves, news, signal/momentum changes).
- [x] Recommendation snapshots store market + news + calendar context (future
      backtesting / similarity dataset).
- [x] Endpoints: `/news/recent`, `/calendar/upcoming`, `/calendar/released`,
      `/timeline/usdmxn`; expanded dashboard.

## Phase 3 — Live market intelligence (current)

Goal: replace mock news and calendar with live providers (mock fallback intact)
and make the analysis explain itself.

- [x] Live news provider interface + **NewsAPI.org** implementation; Finnhub /
      FMP interface stubs selectable via `NEWS_PROVIDER`. Topic-filtered to
      USD/MXN drivers; items classified + stored; mock fallback on error/no key.
- [x] Live economic calendar interface + **Trading Economics** implementation;
      Finnhub stub via `CALENDAR_PROVIDER`. Mock fallback on error/no key.
- [x] Context builder adds events released in the **last 24h**.
- [x] Analysis explains itself: `market_drivers` (per-indicator USD+/MXN+ lean),
      `bullish_factors`, `bearish_factors`, `upcoming_risks`, and a summary that
      names confirming vs opposing indicators.
- [x] Configurable signal weighting engine (`signal_weights.py`): tunable
      per-signal weights (file or `SIGNAL_WEIGHTS` env), weighted USD/MXN scoring,
      and `weighted_contributions` / `conflicting_signals` / `signal_breakdown`
      returned for debugging.
- [x] Persist the new explanatory fields (backtesting dataset grows).
- [x] Dashboard sections: Market Drivers, Latest News, Upcoming Events, Recent
      Releases (24h), Key Risks.
- [x] Secret hygiene: keys sent via headers where possible and scrubbed from all
      error strings; smoke tests assert no key leaks.

## Phase 3.5 — Explainable reasoning engine

Goal: upgrade the analysis from a rules engine into an explainable analyst,
building on (not replacing) the existing architecture.

- [x] **Market regime detection** (`market_regime.py`): primary/secondary regime
      + confidence across Risk On/Off, Fed/Banxico/Inflation/Oil-driven, Trade
      War, Political Risk, Low/High Volatility, Range Bound, Trending.
- [x] **Opportunity grade** (`A+ | A | B | C | D | PASS`) from signal agreement,
      regime, risk, confidence and (volatility-proxied) historical volatility.
- [x] **Explainability**: `what_would_change_my_mind` plus existing
      market_drivers / bullish / bearish / conflicting / upcoming_risks.
- [x] Top-level `usd_score` / `mxn_score` / `net_bias`; active weights persisted
      in `signal_breakdown`.
- [x] Persist regime, grade, grade detail, and reasoning on every snapshot.
- [x] Dashboard cards: Opportunity Grade, Market Regime, What Would Change My
      Mind (alongside the existing weighting/factors cards).
- [x] Smoke tests for regime classification and grading; docs updated.

### Phase 3 follow-ups

- [ ] Real sentiment scoring for news (currently a placeholder lean).
- [ ] Live macro (FRED for DXY/2Y/10Y; oil/gold/VIX/S&P feeds) — currently mocked.
- [ ] Scheduled background polling to build a real time series.
- [ ] Caching + rate-limit handling for providers.

## Phase 4 — Historical intelligence engine (built; sample data)

Goal: backfill market data + economic events, measure how USD/MXN reacted after
each event, and make reactions searchable ("find events like this one") to
replace the placeholder `historical_similarity`. Ships with mock/sample data; no
paid provider required.

Original design: [`PHASE4_HISTORICAL_EVENT_ENGINE.md`](PHASE4_HISTORICAL_EVENT_ENGINE.md)
(table names in the shipped build: `historical_market_snapshots`,
`historical_events`, `historical_event_reactions`, `similarity_matches`).

- [x] History tables — public backfill (`historical_*`) + derived
      `similarity_matches`, kept separate from proprietary `analysis_snapshots`.
- [x] Modular import framework (`services/history/importers.py`): working
      `MockSampleImporter` + CSV/Yahoo/FRED/Alpha Vantage/Polygon stubs.
- [x] Reaction windows (15m/1h/4h/1d/3d/5d) + MFE/MAE/time-to-peak/reversal.
- [x] Similarity engine (configurable `SIMILARITY_WEIGHTS`) + read-only
      `/history/similar`, `/history/statistics`, `/history/events`,
      `/history/probabilities`.
- [x] Probability forecast (target/stretch/stop) + configurable blended
      confidence (`CONFIDENCE_WEIGHTS`) feeding `/analysis/usdmxn`.
- [ ] Wire a real provider (Polygon intraday / FRED / yfinance) to replace the
      sample dataset.

## Phase 4.5 — Strategist narrative (built)

Goal: make every analysis read like a professional FX strategist and give the
Border Currency desk actionable pricing guidance — without new data sources.

- [x] Strategist fields on `/analysis/usdmxn`: `executive_summary`,
      `current_trade_view`, `trader_action`, `why_this_grade`,
      `why_not_higher`, `why_not_lower`, `quote_guidance`, `risk_watchlist`,
      `invalidation_triggers`.
- [x] Concept separation: `confidence` (how sure) vs `opportunity_grade` (how
      attractive). Grade ↔ direction made consistent: **PASS ⇔ NO_TRADE**,
      directional reads floor at `D`, `C`/`D` are bias-only, `B`/`A`/`A+` are
      actionable.
- [x] Quote guidance for desk ops (quote normally / short validity / widen
      spread / avoid aggressive pricing pre-event / requote-beyond-threshold).
- [x] Dashboard "Strategist brief" card; narrative confidence reconciled with the
      Phase 4 blended confidence.

## Phase 5 — Smarter analysis

- [ ] Technical features (moving averages, ATR, RSI) over the stored time series.
- [ ] Real historical_similarity scoring over stored snapshots.
- [ ] LLM-backed narrative analyzer (`OpenAIAnalyzer`) on top of rule-based guardrails.
- [ ] Backtesting harness against stored recommendation snapshots.
- [ ] Confidence calibration.

## Phase 6 — Delivery & access

- [ ] Auth (API keys / sessions).
- [ ] Alerts (email / push / WhatsApp) on signal changes.
- [ ] Public dashboard hardening.

## Later (explicitly out of Phase 1)

- iPhone app.
- SaaS billing / multi-tenant.
- Automated trade execution (would require strict risk controls + broker integration).
