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

### Phase 3 follow-ups

- [ ] Real sentiment scoring for news (currently a placeholder lean).
- [ ] Live macro (FRED for DXY/2Y/10Y; oil/gold/VIX/S&P feeds) — currently mocked.
- [ ] Scheduled background polling to build a real time series.
- [ ] Caching + rate-limit handling for providers.

## Phase 4 — Smarter analysis

- [ ] Technical features (moving averages, ATR, RSI) over the stored time series.
- [ ] Real historical_similarity scoring over stored snapshots.
- [ ] LLM-backed narrative analyzer (`OpenAIAnalyzer`) on top of rule-based guardrails.
- [ ] Backtesting harness against stored recommendation snapshots.
- [ ] Confidence calibration.

## Phase 5 — Delivery & access

- [ ] Auth (API keys / sessions).
- [ ] Alerts (email / push / WhatsApp) on signal changes.
- [ ] Public dashboard hardening.

## Later (explicitly out of Phase 1)

- iPhone app.
- SaaS billing / multi-tenant.
- Automated trade execution (would require strict risk controls + broker integration).
