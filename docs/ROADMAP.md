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

## Phase 4.6 — Multi-horizon outlook (built)

Goal: replace the single expected-duration read with independent outlooks per
timeframe so the desk sees the intraday vs swing picture at a glance.

- [x] `/analysis/usdmxn` returns `time_horizons` — four reads (`1-4 hours`,
      `End of day`, `1-2 days`, `Beyond 2 days`), each re-weighting the same
      signal contributions by timeframe and reporting its own bias / confidence /
      target / stretch / stop / expected_move / rationale / risk_level.
- [x] Longer horizons fold in historical analogs + the blended confidence; swing
      stays low-confidence unless regime/history is strong.
- [x] Dashboard "Time Horizon Outlook" table; main plan labeled "Primary Trade Plan".

## Phase 5 — Evidence-based forecasting engine (built)

Goal: transform the assistant from a weighted rules engine into an
evidence-based FX strategist that backs every read with historical statistics.
Ships on the deterministic sample library; no paid provider required.

- [x] **Pattern library** expanded with a deterministic synthetic 2019–2025
      dataset on top of the curated anchors, so nearest-neighbor matching has a
      deep (50+ event) evidence base.
- [x] **Import framework**: functional `CSVImporter` (`CSV_HISTORY_DIR`) for
      events + reaction paths and a standalone market-series loader; Yahoo /
      FRED / Alpha Vantage / Polygon remain modular stubs.
- [x] **Nearest-neighbor matching**: top-25 analogs with `similarity_score`,
      `distance_score`, and `rank`.
- [x] **Outcome analysis**: average/median/best/worst move, win rate, average
      holding time, average + typical MFE/MAE, maximum drawdown, reversal
      probability.
- [x] **Evidence-based probabilities**: reaches target/stretch/stop + finishes
      positive today/tomorrow/within-5d, each with sample size, a 95% Wilson
      confidence interval, and a historical basis.
- [x] **Confidence model**: configurable weighted blend with a full per-term
      `explanation`, the exact `formula`, and the six conceptual `inputs`.
- [x] **Strategist evidence brief** (`evidence_summary`) + setup percentile rank.
- [x] **Explain every number** (`explanations`): trade score, confidence,
      opportunity grade, historical similarity, probability.
- [x] Dashboard **Historical Evidence** panel + "How these numbers are
      calculated" card; everything persisted on the analysis snapshot.

## Phase 5.5 — Real data sources & source labeling (built)

Goal: move off mock intelligence where real feeds are configured, and label
every source so the dashboard never implies sample/mock data is real. Mock
fallback is preserved; the reasoning engine is unchanged beyond consuming the
real feed when available.

- [x] **Live news** via NewsAPI (`NEWS_API_KEY`) with mock fallback — `live`
      on success, `fallback` on error, `mock` when unconfigured.
- [x] **Importable calendar**: new `CSVCalendarProvider` (`CALENDAR_PROVIDER=csv`
      + `CALENDAR_CSV_PATH`) loads a real calendar export with no paid key,
      tagged `imported`; live Trading Economics (`CALENDAR_API_KEY`) unchanged.
- [x] **`data_sources`** block on `/analysis/usdmxn` (market / news / calendar /
      historical) feeding per-source dashboard badges
      (`live` · `imported` · `fallback` · `mock` · `sample` · `backfilled`).
- [x] No API keys logged (scrubbed); smoke tests cover provider source tags,
      CSV import + fallback, and the analysis `data_sources` contract.

## Phase 5.6 — Connect live market intelligence providers (built)

Goal: move real feeds into production, per field, while preserving the mock
fallback architecture and the reasoning engine.

- [x] **Finnhub news** (`NEWS_PROVIDER=finnhub`): live `general` + `forex`
      feeds replacing MockWire, filtered to USD/MXN topics with a
      `relevance_score` (0 discarded); key sent via `X-Finnhub-Token` header.
- [x] **FRED macro**: live US 2Y (`DGS2`) + US 10Y (`DGS10`) yields.
- [x] **Alpha Vantage macro**: live WTI oil + gold (XAU/USD); DXY/VIX/S&P
      attempted and retained as fallback when the free tier can't serve them,
      with the reason logged (keys scrubbed).
- [x] **Per-field source transparency**: `market.sources` maps every field to
      `live`/`fallback`/`mock`; dashboard renders a badge per market field.
      Range checks + a `MACRO_CACHE_SECONDS` cache respect provider rate limits.
- [x] **Persistence**: `MarketSnapshot.sources` + `NewsItem.relevance_score`
      (provider, fetch time, headline, tags, sentiment already stored).
- [x] Smoke tests cover Finnhub filtering, FRED/Alpha Vantage per-field live +
      fallback, all-mock without keys, and that errors never expose API keys.

## Phase 5.1 — Market intelligence infrastructure (built)

Goal: a professional data layer that minimizes API usage, respects market
hours, and continuously builds the historical database.

- [x] **Market hours** (`services/market_hours.py`): global FX week (Sun 21:00 →
      Fri 21:00 UTC) with `OPEN`/`CLOSED`/`WEEKEND`/`HOLIDAY`/`EARLY_CLOSE`/
      `MAINTENANCE`, plus `last_market_close` / `next_market_open` /
      `next_expected_refresh`. Holiday/early-close/maintenance are extensible
      frameworks, not hardcoded weekends.
- [x] **Intelligent cache** (`services/cache_manager.py`): serve fresh-enough
      cache before live; **never request USD/MXN while closed**; serve latest
      cache before mock; mock only when no cache exists.
- [x] **Configurable refresh policies** (`REFRESH_POLICIES`) with market-gated
      keys, plus `policies_view` + a prepared `RefreshScheduler` interface (no
      background execution yet).
- [x] **Automatic historical capture**: every successful live refresh writes a
      `historical_market_snapshots` row (no second process).
- [x] **Market metadata** on `/market/usdmxn` (status, reason, cached,
      fetched_at, age, next refresh/open, is_stale) + `/market/status`.
- [x] **Provider health** (healthy/rate_limited/offline/using_cache/
      using_fallback) on `/analysis/usdmxn` and the dashboard.
- [x] **Analysis awareness**: closed-market note; prices marked as latest
      session while news/calendar/historical/regime/strategist still evaluate.
- [x] Dashboard **Market status** + **Provider health** panels. Smoke tests
      cover weekend no-fetch, holiday closure, cache-before-live,
      cache-before-mock, and automatic historical capture.

## Phase 5.2 — Recommendation outcome tracking (built)

Goal: store every AI market recommendation and later evaluate whether it was
correct — paper signals, kept separate from real trades.

- [x] `recommendations` table (lean + indexed on timestamp/direction/confidence/
      opportunity_grade/last_evaluated_at); a row is stored on every
      `/analysis/usdmxn`.
- [x] `recommendation_outcomes` table (per horizon: 1h/4h/end_of_day/1d/2d/5d)
      with return, direction_correct, target/stretch/stop hits, MFE/MAE; unique
      per (recommendation, horizon).
- [x] `recommendation_evaluator.py`: bounded `evaluate_due` (never heavy on
      dashboard load) + fast `performance_summary` aggregation.
- [x] Endpoints: `/recommendations/recent`, `/recommendations/performance`,
      `POST /recommendations/evaluate`.
- [x] Dashboard Model Performance panel (win/target/stop rates, avg return,
      breakdowns by confidence bucket, grade, horizon).
- [x] Real trades stay separate; a future real-trade row can link via
      `recommendation_id`. Smoke tests cover storage, multi-horizon scoring, and
      the fast performance read.

## Phase 5.3 — AI Research Lab & paper hedge performance (built)

Goal: a self-evaluating market intelligence platform — every recommendation is a
permanent research observation, later scored and used to measure model quality.

- [x] Versioned recommendation repository (`recommendation_uuid`, model/
      reasoning/weighting/historical engine versions; factors, regime,
      volatility, news category, trade plan) — separate from public history and
      real trades.
- [x] Evaluator adds time-to-target/stop, holding time, and per-horizon paper
      hedge P/L; completed evaluations are never recomputed.
- [x] Paper hedge (SIMULATED): $100k notional, $40 round-trip cost, BUY_USD/
      SELL_USD only (never PASS/NO_TRADE).
- [x] `research_lab.py`: accuracy breakdowns (confidence/grade/regime/news/
      similarity/volatility/horizon/model version), confidence calibration,
      signal stability + drift, top/weakest drivers, provider reliability, and
      self-assessment observations (never auto-tunes weights).
- [x] Monthly performance with full P/L + breakdowns.
- [x] Endpoints: `/research/summary|calibration|drivers|model-performance|
      performance`, `/performance/monthly|summary|recommendations`.
- [x] Dashboard AI Research Lab + Paper hedge panels labeled SIMULATED PAPER
      PERFORMANCE. Indexed for hundreds of thousands of rows. Smoke tests cover
      hedge math, research summary/calibration/model perf, and monthly totals.

## Phase 5.4 — Decision quality engine (built)

Goal: decide not only direction but whether a trade is worth taking now vs
waiting. Decision support only — not trading execution.

- [x] `decision_quality.py`: trade quality score (0-100, separate from
      confidence) + label (Excellent/Good/Marginal/Poor/Wait), a weighted blend
      of signal strength, historical evidence, reward/risk, event risk,
      volatility fit, model track record, and similar-recommendation paper P/L.
- [x] Conservative gate: `should_trade_now` only when actionable + grade B or
      better + reward/risk ≥ 1.0; PASS/NO_TRADE -> Wait, null EV, with
      reason_to_wait / better_entry_conditions / what_to_watch_next.
- [x] Reward/risk, breakeven win rate, and expected value on $100k notional net
      of the $40 round-trip cost.
- [x] Similar-recommendation track record with explicit "not enough history yet"
      flagging (provisional rates excluded from the score).
- [x] Selective trading analysis (top 10/20/30%, grade A/B+, confidence > 70/80)
      with win rate, net/avg P/L, max drawdown, return on notional; no claims at
      zero samples.
- [x] Endpoints `/decision/quality|selective-performance|current-context`;
      `decision_quality` embedded in `/analysis/usdmxn` (and they agree).
- [x] Dashboard Decision quality panel labeled decision support only. Smoke
      tests cover PASS/NO_TRADE wait, weak-grade wait, B/A trade, EV cost
      deduction, not-overstated history, zero-sample selectivity, and agreement.

## Phase 7 — Smarter analysis

- [ ] Technical features (moving averages, ATR, RSI) over the stored time series.
- [ ] Wire a real provider (Polygon intraday / FRED / yfinance) to replace the
      sample dataset behind the Phase 5 evidence engine.
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
