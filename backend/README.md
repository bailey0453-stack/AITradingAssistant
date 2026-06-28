# AI Trading Assistant — Backend (Phase 5)

Backend-only **USD/MXN market intelligence engine**. It collects market + macro
inputs, **live news**, and a **live economic calendar**, builds a structured
context, runs an **explainable reasoning engine** that classifies the market
regime, scores weighted USD-vs-MXN evidence, grades the opportunity, and explains
*why* it sees an edge (and what would change its mind). The **historical
intelligence engine** then answers *"have we seen conditions like this before,
and what usually happened next?"* — ranking past events by similarity and
producing aggregate stats + probabilities. It persists everything (market
snapshots, news, full recommendation snapshots, and a backfilled historical
dataset) and exposes a JSON API plus a dashboard.

> Scope: **no iPhone app, no SaaS billing, no auto-trading.** This is a
> read-only intelligence service that produces a directional view, not orders.

## Features

- FastAPI service. Endpoints:
  - `GET /health` — service status.
  - `GET /market/usdmxn` — expanded market snapshot (stored).
  - `GET /analysis/usdmxn` — full recommendation + context + timeline (stored).
  - `GET /news/recent` — structured recent news (stored).
  - `GET /calendar/upcoming` — upcoming tracked economic events.
  - `GET /timeline/usdmxn` — recent event/market/signal timeline (read-only).
  - `GET /history/events` — backfilled historical events.
  - `GET /history/similar` — past events most like the current context.
  - `GET /history/statistics` — aggregate stats over the similar events.
  - `GET /history/probabilities` — probability of hitting target/stop levels.
  - (`/market/usdmxn/history`, `/analysis/usdmxn/history`, `/calendar/released`.)
- **Expanded market snapshot**: USD/MXN, inverse, DXY, US 2Y, US 10Y, WTI oil,
  gold, S&P futures, VIX, provider, source, timestamp.
- **News ingestion (live)**: modular provider; **NewsAPI.org** is the initial
  live implementation (Finnhub / FMP are interface stubs). Items carry headline,
  summary, source, url, published_at, sentiment *(placeholder)*,
  affected_currencies, importance, tags. Filtered to USD/MXN-moving topics and
  stored in the DB (deduplicated). Falls back to mock news if no key or on error.
- **Economic calendar (live)**: modular provider; **Trading Economics** is the
  initial live implementation. Tracks US CPI/PPI/NFP/GDP/Retail Sales/FOMC/Fed
  speeches/Treasury auctions and Banxico/Mexico CPI/GDP/employment. Events carry
  forecast/previous/actual/importance/currency impact and `upcoming|released`.
  Falls back to mock events if no key or on error.
- **Context builder** assembles market + recent news + upcoming events + events
  released in the **last 24h** + recent analyses into one object for the analyzer.
- **Configurable signal weighting** (`signal_weights.py`): every signal (rate
  decisions, CPI/PPI/NFP/GDP, yields, DXY, momentum, oil/gold/S&P/VIX, news
  categories, technicals) has a tunable weight. The analyzer scores weighted
  USD-vs-MXN evidence — it does **not** treat inputs equally — and returns the
  `weighted_contributions`, `conflicting_signals`, and a `signal_breakdown`
  (USD/MXN/net scores) for debugging. Tune via the file or `SIGNAL_WEIGHTS`.
- **Explainable reasoning engine** (Phase 3.5): on top of the weighted signal it
  adds **market regime detection** (`market_regime.py` → primary/secondary regime
  + confidence), an **opportunity grade** (`A+ | A | B | C | D | PASS`) derived
  from signal agreement, regime, risk, confidence and volatility, and a
  **`what_would_change_my_mind`** list of concrete, falsifiable invalidation
  conditions.
- **Strategist narrative** (Phase 4.5): every analysis also reads like a
  professional FX strategist — `executive_summary`, `current_trade_view`,
  `trader_action`, `why_this_grade` / `why_not_higher` / `why_not_lower`,
  `quote_guidance` (Border Currency desk pricing), `risk_watchlist`, and
  `invalidation_triggers`. It keeps two concepts distinct: **confidence** (how
  sure the system is) vs **opportunity_grade** (how attractive the trade is). The
  grade is consistent with direction: **PASS ⇔ NO_TRADE**, a directional read
  floors at `D`, `C`/`D` are bias-only (low quality), and `B`/`A`/`A+` support an
  active recommendation.
- **Multi-horizon outlook** (Phase 4.6): alongside the single Primary Trade Plan,
  `/analysis/usdmxn` returns `time_horizons` — four independent reads (`1-4 hours`,
  `End of day`, `1-2 days`, `Beyond 2 days`). Each re-weights the same signal
  contributions by timeframe (short horizons lean on momentum/DXY/yields/oil/VIX
  + news; 1-2 days emphasizes upcoming Fed/Banxico/calendar + historical analogs;
  beyond 2 days stays low-confidence unless regime/history is strong) and reports
  its own `bias`, `confidence`, `target`/`stretch_target`/`stop`, `expected_move`,
  `rationale`, and `risk_level`. Horizons may show a directional lean even when
  the primary recommendation is `NO_TRADE`/`PASS`.
- **Historical intelligence engine** (Phase 4, `services/history/`): backfills a
  historical dataset (sample data out of the box, paid providers optional),
  measures USD/MXN reactions after each event over fixed windows (15m/1h/4h/1d/
  3d/5d, plus MFE/MAE/time-to-peak/reversal), and ranks past events by similarity
  to the current context. Returns aggregate stats (avg/median move, win rate,
  expected range/duration, typical MFE/MAE), a **probability forecast** for
  hitting target/stop levels, and feeds a **configurable blended confidence**
  (signal + historical + regime + volatility + data quality).
- **Evidence-based forecasting engine** (Phase 5): turns the assistant into an
  evidence-led FX strategist. It expands the pattern library to a deep,
  deterministic sample set (curated anchors + a synthetic 2019–2025 library) so
  every analysis can run **nearest-neighbor matching over the top 25 analogs**
  (each match carries a `similarity_score`, `distance_score`, and `rank`).
  - **Outcome analysis** adds best/worst move, average + median move, win rate,
    average holding time, average + typical MFE/MAE, **maximum drawdown**, and
    **reversal probability** over the matched set.
  - **Evidence-based probabilities**: `reaches target / stretch / stop` plus
    `finishes positive today / tomorrow / within 5 days`, each with a
    **sample size**, a **95% Wilson confidence interval**, and a plain-language
    **historical basis** (`probabilities.evidence`).
  - **Confidence model** stays a configurable weighted blend (current signals +
    historical evidence + regime + volatility + data completeness), but now
    exposes a full **`explanation`** (each term and contribution), the exact
    **`formula`**, and the six conceptual **`inputs`** (incl. news quality &
    calendar certainty).
  - **Strategist evidence brief** (`evidence_summary`): e.g. *"Historically,
    conditions similar to today occurred 143 times. USD strengthened in 108 of
    those cases (75.5%). Average move +0.46%, median +0.39%. Average holding 31
    hours. Largest adverse excursion -0.18%. Current setup ranks in the 91st
    percentile of bullish historical setups."*
  - **Explain every number** (`explanations`): trade score, confidence,
    opportunity grade, historical similarity, and probability each expose how
    they were calculated.
  - **Import framework** is genuinely capable of loading from CSV (functional
    `CSVImporter` via `CSV_HISTORY_DIR`) and standalone market series, with
    Yahoo / FRED / Alpha Vantage / Polygon as modular stubs. No paid provider is
    required — the sample library works out of the box.
- **Richer analysis**: direction, trade_score, market_bias, confidence,
  momentum, historical_similarity (placeholder), risk_level, summary (now
  explains which indicators confirm vs push back), key_drivers, **market_drivers**
  (per-indicator USD+/MXN+ lean), **bullish_factors**, **bearish_factors**,
  **conflicting_signals**, **upcoming_risks**, **what_would_change_my_mind**,
  **market_regime**, **opportunity_grade**, top-level **usd_score / mxn_score /
  net_bias**, entry/target/stretch/stop, expected_move, expected_duration,
  invalidation_level, risk_notes — plus an **event timeline**.
- **Recommendation store**: every analysis persists market + news + calendar
  context + the recommendation — the future backtesting / similarity dataset.
- Modular provider layer with **mock fallback** everywhere; SQLite by default,
  Postgres-ready via `DATABASE_URL`.
- HTML dashboard at `/`.

## Architecture

```
request → router → services → models (DB)

routers/         thin HTTP layer (market, analysis, news, calendar, timeline, health)
services/
  market_data.py   USD/MXN + macro providers (mock | live OXR | fallback)
  news.py          news provider (mock | live NewsAPI | fallback; Finnhub/FMP stubs)
  calendar.py      economic calendar provider (mock | live Trading Economics | fallback)
  signal_weights.py  configurable weighting engine (the only place weights live)
  market_regime.py   regime detection (Risk On/Off, Fed/Banxico/Inflation/Oil-driven, ...)
  signals.py       directional view + trade levels (delegates scoring to weights)
  ai_analysis.py   analyzer: recommendation + regime + opportunity grade + explanation
  context_builder.py  gathers context + builds the event timeline
  history/         historical intelligence engine (Phase 4)
    importers.py          modular backfill (mock/sample + CSV/Yahoo/FRED/AV/Polygon stubs)
    historical_prices.py  reaction-window math (15m..5d, MFE/MAE, reversal)
    historical_events.py  read/seed events + reactions from the DB
    similarity_engine.py  feature vectors + weighted similarity + ranking
    historical_statistics.py  aggregate stats, probability forecast, confidence blend
  secrets.py       scrubs API keys out of any log/error string
models/          MarketSnapshot, NewsItem, AnalysisSnapshot (snapshots.py);
                 HistoricalMarketSnapshot, HistoricalEvent, HistoricalEventReaction,
                 SimilarityMatch (history.py)
```

### Data separation (kept in distinct tables)

| Dataset | Table(s) | Nature |
| --- | --- | --- |
| Public historical backfill | `historical_market_snapshots`, `historical_events`, `historical_event_reactions` | Public/free, re-importable |
| Derived similarity cache | `similarity_matches` | Derived; links public events ↔ (optional) a recommendation |
| Proprietary live recommendations | `analysis_snapshots` | The live AI history |
| Future Border Currency trade outcomes | *(not modeled here)* | Kept out by design |

These are never merged into one table — the similarity cache references the
others by id only.

Flow for `GET /analysis/usdmxn`: capture market snapshot + news (stored) →
`build_context` (DB news/analyses + calendar provider) → `build_timeline` →
`analyzer.analyze(market, news, calendar, recent_analyses)` → store
`AnalysisSnapshot` with context + timeline → return.

## Provider system & mock fallback

Each external dependency sits behind an interface + factory, selected by config:

| Service | Factory | Live when | Else |
| --- | --- | --- | --- |
| USD/MXN spot | `get_market_data()` | `USE_MOCK_DATA=false` **and** `FX_API_KEY` set | `mock`; `fallback` on error |
| Macro (2Y/10Y) | FRED via `macro_data` | `USE_MOCK_DATA=false` **and** `FRED_API_KEY` set | per-field `fallback`; else `mock` |
| Macro (DXY/gold/oil/VIX/S&P) | Alpha Vantage via `macro_data` | `USE_MOCK_DATA=false` **and** `ALPHA_VANTAGE_API_KEY` set | per-field `fallback`; else `mock` |
| News | `get_news_provider()` | `USE_MOCK_DATA=false` **and** `NEWS_API_KEY` set (`NEWS_PROVIDER=newsapi`\|`finnhub`) | mock; `fallback` on error |
| Calendar | `get_calendar_provider()` | `USE_MOCK_DATA=false` **and** `CALENDAR_API_KEY` set, **or** `CALENDAR_PROVIDER=csv` + `CALENDAR_CSV_PATH` | mock; `fallback` on error |
| Analyzer | `get_analyzer()` | `USE_MOCK_DATA=false` **and** `OPENAI_API_KEY` set | rule-based |

Every live provider degrades safely: if a fetch fails or the key is missing,
the service returns mock data (tagged `source="fallback"` for market) and never
breaks.

### Live macro indicators (per-field)

Macro drivers are fetched **independently per field**, so one unavailable
symbol degrades only that value:

- **FRED** (`FRED_API_KEY`) → US 2Y (`DGS2`) and US 10Y (`DGS10`) yields.
- **Alpha Vantage** (`ALPHA_VANTAGE_API_KEY`) → WTI oil and gold (XAU/USD).
  DXY, VIX and S&P have no clean free-tier endpoint, so they are attempted and,
  when unavailable, retained as the existing (mock) value tagged `fallback` —
  the reason is logged (with keys scrubbed).

Every macro fetch is range-checked (a wrong-scale value is rejected) and cached
for `MACRO_CACHE_SECONDS` (default 600s). **Note:** Alpha Vantage's free tier is
~25 requests/day; caching keeps the app within that budget, and fields fall
back to mock if the limit is hit. `/market/usdmxn` returns a `sources` map
(`{field: live|fallback|mock}`) and the dashboard shows a badge per field.

### Market intelligence infrastructure (Phase 5.1)

A professional data layer minimizes API usage, respects market hours, and
continuously builds the historical database.

- **Market hours** (`services/market_hours.py`): the global FX week runs
  continuously from **Sunday 21:00 UTC** to **Friday 21:00 UTC**. State is one
  of `OPEN` / `CLOSED` / `WEEKEND` / `HOLIDAY` / `EARLY_CLOSE` / `MAINTENANCE`,
  returned with `market_reason`, `last_market_close`, `next_market_open`, and
  `next_expected_refresh`.
- **Holiday framework**: pass a `MarketCalendar` (holidays / early closes /
  maintenance windows) or set `MARKET_HOLIDAYS='["2026-01-01"]"`. Nothing is
  hardcoded to weekends only.
- **Cache strategy** (`services/cache_manager.py`): if a value is within its
  refresh interval it's reused; if expired **and the market is open** a live
  fetch runs; **while the market is closed USD/MXN is never requested** — the
  latest stored session is served. On provider failure the latest cache is
  served, and mock data is used only when no cache exists at all.
- **Refresh policies** (configurable via `REFRESH_POLICIES`, minutes): USD/MXN
  60m (market-gated), news 5m, calendar 30m, treasury/DXY/gold/oil/VIX 15m.
  Market-instrument keys are gated to market hours.
- **Automatic historical capture**: every successful **live** refresh writes a
  `historical_market_snapshots` row (timestamp, provider, market data, status,
  source) — the foundation for similarity analysis, with no second process.
- **Market metadata**: `/market/usdmxn` returns `market_status`,
  `market_reason`, `provider`, `source`, `cached`, `fetched_at`, `cached_at`,
  `age_minutes`, `refresh_interval_*`, `next_refresh`, `last_market_close`,
  `next_market_open`, and `is_stale`. `/market/status` returns the state +
  policies + provider health without forcing a fetch.
- **Provider health**: each provider reports `healthy` / `rate_limited` /
  `offline` / `using_cache` / `using_fallback`; surfaced on `/analysis/usdmxn`
  (`provider_health`) and the dashboard's Provider Health panel.
- **Analysis awareness**: when the market is closed, `/analysis/usdmxn` adds a
  `market_state` block and a `market_status_note` clarifying that prices are the
  latest session (not moving) while news, calendar, historical evidence, regime,
  and the strategist view are still evaluated.
- **Future scheduler**: `cache_manager.RefreshScheduler.planned_jobs()` enumerates
  the periodic refreshes a future scheduler (e.g. Vercel Cron) would run.
  Nothing runs in the background yet — the interface is only prepared.

### Recommendation outcome tracking (Phase 5.2)

Every `/analysis/usdmxn` signal is stored as a **paper recommendation** (model
signal) and later scored against the real USD/MXN price path. These are kept
**separate from any real trade** — a future real-trade table can link back via
`recommendation_id`, but the datasets never merge.

- `recommendations` — one lean, indexed row per signal (spot price, direction,
  confidence, opportunity_grade, trade_score, market_regime, target/stretch/stop,
  time_horizons, key_drivers, historical_similarity, strategist). Indexed on
  `created_at`, `direction`, `confidence`, `opportunity_grade`, `last_evaluated_at`.
- `recommendation_outcomes` — one row per (recommendation, horizon) with
  `evaluated_at`, `spot_at_evaluation`, `return_pct`, `direction_correct`,
  `target_hit`, `stretch_hit`, `stop_hit`, `max_favorable_excursion`,
  `max_adverse_excursion`. Horizons: `1h`, `4h`, `end_of_day`, `1d`, `2d`, `5d`.
- **Evaluator** (`services/recommendation_evaluator.py`): scores recommendations
  only once a horizon's time has passed and a post-horizon price exists. It is
  **batched and bounded** (`evaluate_due(limit=...)`) so it never runs as a heavy
  calculation on a dashboard load.
- **Endpoints**: `GET /recommendations/recent`, `GET /recommendations/performance`
  (fast aggregate over already-scored outcomes), `POST /recommendations/evaluate`
  (score due rows — call manually or from a future scheduler).
- **Dashboard**: a Model Performance panel (total, win rate, target/stop hit
  rates, average return; breakdowns by confidence bucket, grade, and horizon).
  The dashboard only *reads* performance; it never triggers evaluation.

> Durable accumulation needs a persistent `DATABASE_URL` (Postgres). On the
> default ephemeral SQLite the recommendation/outcome history resets per cold
> start.

### AI Research Lab & paper hedge performance (Phase 5.2+)

A self-evaluating layer that turns every recommendation into a permanent
research observation, scores it, and measures model quality over time.

- **Versioned repository**: each recommendation gets a `recommendation_uuid` plus
  `model_version` / `reasoning_engine_version` / `weighting_profile` /
  `historical_engine_version` (see `app/versions.py`), the market snapshot,
  strategist narrative, factors (bullish/bearish/conflicting), regime,
  volatility, news category, time horizons, and the primary trade plan. Kept
  separate from public historical data and from real Border Currency trades; a
  future real trade can reference `recommendation_uuid`.
- **Evaluator** (`recommendation_evaluator.py`): scores 1h/4h/end_of_day/1d/2d/5d
  with direction-correct, target/stretch/stop hits, return %, MFE/MAE, time to
  target, time to stop, and holding time. Completed evaluations are never
  recomputed (unique per recommendation+horizon).
- **Paper hedge (SIMULATED — never a real trade)**: each actionable BUY_USD /
  SELL_USD becomes a $100,000 notional hedge with $20 entry + $20 exit = $40
  cost; PASS/NO_TRADE generate none. Stored: `hedge_return_pct`,
  `gross_pnl_usd`, `net_pnl_usd`.
- **Research Lab** (`research_lab.py`): overall accuracy; accuracy by confidence
  bucket / grade / regime / news category / historical-similarity bucket /
  volatility / horizon / model version; confidence calibration (predicted vs
  actual); signal stability + recommendation drift; top/weakest drivers;
  historical-similarity accuracy; provider reliability; and **self-assessment
  observations** (observations only — weights are never changed automatically).
- **Monthly performance**: totals, actionable count, win rate, gross/costs/net
  P/L, average P/L, return on notional, best/worst trade, with breakdowns by
  confidence/grade/regime/model version/horizon.
- **Endpoints**: `GET /research/summary`, `/research/calibration`,
  `/research/drivers`, `/research/model-performance`, `/research/performance`,
  `GET /performance/monthly`, `/performance/summary`, `/performance/recommendations`.
- **Dashboard**: an **AI Research Lab** panel and a **Paper hedge performance**
  panel, both clearly labeled **SIMULATED PAPER PERFORMANCE**. All reads are
  cheap aggregates; evaluation runs only via `POST /recommendations/evaluate`.
- **Indexes** for scale (hundreds of thousands of rows): `created_at`,
  `recommendation_uuid`, `model_version`, `confidence`, `opportunity_grade`,
  `regime`, `evaluation_status`, `evaluated_at`.

### Decision quality engine (Phase 5.3)

Decides not just *direction* but whether a trade is worth taking **now vs
waiting**. Decision support only — never trading execution.

- **Trade quality score + label** (`decision_quality.py`): a 0-100 score
  *separate from confidence*, a weighted blend of signal strength, historical
  evidence, reward/risk, event risk, volatility fit, model track record for
  similar signals, and paper-hedge performance for similar recommendations.
  Missing inputs renormalize the weights. Label: **Excellent / Good / Marginal
  / Poor / Wait**.
- **Conservative gate**: `should_trade_now` is true only for a genuinely
  tradeable setup (actionable direction, grade **B or better**, and reward/risk
  ≥ 1.0). PASS / NO_TRADE always produce `should_trade_now=false`, label
  **Wait**, and a null expected value. When waiting, the engine returns
  `reason_to_wait`, `better_entry_conditions`, and `what_to_watch_next`.
- **Reward / risk & expected value**: `reward_to_target`, `risk_to_stop`,
  `reward_risk_ratio`, `minimum_required_win_rate` (breakeven), and
  `expected_value_usd` on $100,000 notional **net of the $40 round-trip cost**.
- **Similar track record**: count, win rate, avg P/L, target/stop hit rates for
  past recommendations matching direction (+ grade). Below the sample threshold
  it is clearly flagged ("Not enough similar history yet"), the rates are marked
  provisional, and they are excluded from the quality score.
- **Selective trading analysis**: "if we only traded the top 10/20/30%, grade A
  or better, grade B or better, confidence > 70/80" — trades, win rate, net P/L,
  avg P/L, max drawdown, return on notional. Reads scored outcomes only and
  makes no claims at zero samples.
- **Endpoints**: `GET /decision/quality`, `GET /decision/selective-performance`,
  `GET /decision/current-context`. The block is also embedded in
  `/analysis/usdmxn` as `decision_quality`, and `/decision/quality` agrees with
  the latest analysis.
- **Dashboard**: a **Decision quality** panel (score, label, should-trade-now,
  reward/risk, expected value, reason to wait/trade, component breakdown,
  similar track record, selective-trading table), labeled decision support only.

### Evidence & provenance engine (Phase 5.4)

Every major analysis output is annotated with *where it came from* and *how
trustworthy it is*, so an AI estimate is never mistaken for real market data.

- **Evidence levels** (`provenance.py`): `5 measured` (stored recommendation
  outcomes) · `4 historical` (historical market DB) · `3 live` (current provider)
  · `2 cached` (previously verified live) · `1 estimated` (reasoning engine) ·
  `0 sample` (mock/demo).
- **Provenance metadata**: `/analysis/usdmxn` returns a `provenance` map where
  each field is `{value, source, evidence_level, badge, explanation, …}` —
  covering spot rate + macro fields (live/cached/sample), the trade plan
  (entry/target/stretch/stop/expected move/probabilities = estimated),
  confidence + decision quality (estimated), historical similarity / win rate
  (historical or sample), and recommendation accuracy / similar track record
  (measured when outcomes exist).
- **Evidence summary** (`evidence_overview`): counts and shares per source
  (live / cached / measured / historical / estimated / sample) — an instant read
  on how much of today's analysis is evidence vs inference.
- **Measured vs estimated stay separate**: recommendation accuracy is *measured*;
  the historical-similarity score is *historical/sample* — never conflated.
- **Auto-upgrade (provenance only)**: estimated trade-plan metrics are *labeled*
  measured once enough similar recommendation history exists. The value is never
  changed — only the provenance.
- **Historical database label**: "Sample Historical Database" (sample),
  "Historical Database" (imported/backfilled), or "Measured Recommendation
  History" (stored outcomes).
- **Dashboard**: an **Evidence summary** card plus self-explaining evidence
  badges (hover tooltips) beside the spot rate, trade plan, historical
  similarity/win rate, and a MEASURED badge on recommendation accuracy.

### Finnhub news

Set `NEWS_PROVIDER=finnhub` + `NEWS_API_KEY` to pull live financial news from
Finnhub (`general` + `forex` categories). Articles are filtered to USD/MXN
topics (Fed/FOMC/Powell, Banxico, Mexico, peso, USD/MXN, CPI/PPI/NFP,
Treasury/inflation, oil, tariffs/US–Mexico trade); each keeps a
`relevance_score` (0–100) and anything scoring 0 is discarded. The key is sent
in the `X-Finnhub-Token` header so it never reaches a URL or log line.

### Data-source labeling

Nothing on the dashboard implies sample/mock data is real. `/analysis/usdmxn`
returns a `data_sources` block and the dashboard renders a badge per source:

| Source | Possible labels |
| --- | --- |
| `market` | `live` · `mock` · `fallback` (overall = USD/MXN spot; per-field in `market.sources`) |
| `news` | `live` · `mock` · `fallback` |
| `calendar` | `live` · `imported` (CSV) · `mock` · `fallback` |
| `historical` | `live` · `backfilled` · `sample` |

The **importable calendar** lets you load a real calendar export with no paid
key: set `CALENDAR_PROVIDER=csv` and `CALENDAR_CSV_PATH=/path/to/calendar.csv`
(header columns: `event,country,release_time,forecast,previous,actual,
importance,currency_impact`). It still falls back to mock data if the file is
missing or empty, and is tagged `imported` when it loads. **API keys are never written to logs** — keys are sent via request
headers (FX `Authorization`, NewsAPI `X-Api-Key`) or, where a provider requires
a query param (Trading Economics), scrubbed from every outbound error string via
`services/secrets.py`. Choose the live implementation with `NEWS_PROVIDER` /
`CALENDAR_PROVIDER`.

## Signal weighting engine

All scoring weights live in one place — `app/services/signal_weights.py` — so
the model is tunable without touching the analysis engine. Each signal is turned
into a *signed, weighted contribution* (`weight × strength`, direction `USD` or
`MXN`); the engine sums each side, takes the net, and derives Trade Score,
Confidence, Market Bias, and Key Drivers, while flagging conflicting signals.

Default weights (0–10):

| Signal | Weight | Signal | Weight |
| --- | --- | --- | --- |
| Fed Rate Decision | 10 | Banxico Rate Decision | 10 |
| US CPI | 9 | US PPI | 8 |
| US Nonfarm Payrolls | 9 | US GDP | 8 |
| Mexico CPI | 9 | Mexico GDP | 8 |
| Treasury Yield (2Y/10Y) | 8 | DXY | 8 |
| USD/MXN Momentum | 7 | Oil | 7 |
| Gold | 5 | S&P Futures | 5 |
| VIX | 6 | Trade/Tariff News | 8 |
| Political News | 5 | General Financial News | 4 |
| Technical Indicators | 5 | | |

Tune by editing the file, or override at runtime with the `SIGNAL_WEIGHTS` env
var (a JSON object; unknown keys are ignored):

```bash
SIGNAL_WEIGHTS='{"dxy": 9, "oil": 6, "us_cpi": 10}'
```

`GET /analysis/usdmxn` returns `weighted_contributions` (sorted strongest-first),
`conflicting_signals`, and `signal_breakdown` (`usd_score`, `mxn_score`,
`net_score`, `trade_threshold`, `weights_version`, and the active `weights`) for
debugging and tuning. The USD/MXN scores are also surfaced at the top level as
`usd_score`, `mxn_score`, and `net_bias`.

## Reasoning engine (Phase 3.5)

The analyzer turns the weighted score into an *explainable* read. It does not
replace the rules engine — it layers on top of it.

**Market regime** (`app/services/market_regime.py`) re-reads the same evidence
(volatility, equities/gold, oil, calendar events, news categories, momentum) and
classifies the tape. Each regime accumulates a transparent score from named
pieces of evidence; the strongest two become `primary`/`secondary` with a
`confidence` (0–100) and a short `rationale`. Possible regimes: *Risk On, Risk
Off, Fed Driven, Banxico Driven, Inflation Driven, Oil Driven, Trade War,
Political Risk, Low/High Volatility, Range Bound, Trending.*

**Opportunity grade** (`A+ | A | B | C | D | PASS`) blends signal agreement
(net vs total weight), confidence, and trade score into a 0–100 composite, then
deducts penalties for risk level, conflicting signals, and elevated volatility
(a historical-volatility proxy). Uncertain regimes cap the top grade, and a
`NO_TRADE` direction always grades `PASS`. The full breakdown (`score`,
`reasons`, `components`) is returned as `opportunity_grade_detail`.

**`what_would_change_my_mind`** lists concrete, falsifiable conditions that would
weaken or flip the view — e.g. price through the stop, an opposing signal
strengthening, an imminent high-impact release, or a regime shift.

All of the above (`market_regime`, `opportunity_grade`,
`opportunity_grade_detail`, `what_would_change_my_mind`) plus the persisted
`signal_breakdown` (including the active weights) are stored on every
`AnalysisSnapshot`.

## Historical intelligence engine (Phase 4)

Answers *"have we seen conditions like this before, and what usually happened
next?"* It works with **mock/sample data out of the box** — no paid provider
required — and the same code path accepts real data later.

### Historical database

Four new tables (kept separate from the live recommendation history):

- `historical_market_snapshots` — backfilled USD/MXN + macro time series.
- `historical_events` — economic events with `forecast/actual/surprise(_z)`.
- `historical_event_reactions` — how USD/MXN reacted after each event
  (windowed returns 15m/1h/4h/1d/3d/5d, max favorable/adverse excursion, time to
  peak, reversal behavior, data completeness) **plus the pre-event market
  context** (the similarity feature vector).
- `similarity_matches` — a cache of "events like now" comparisons; references
  public events and, optionally, the proprietary recommendation that triggered
  them. The datasets are never mixed into one table.

Every row records `source` + `source_quality` so a reconstructed sample path is
never confused with a vendor's real intraday tick.

### Reaction-window math (`historical_prices.py`)

Pure functions over a price *path* (hours-after-event → price): windowed percent
returns, max favorable/adverse excursion **relative to the net reaction
direction**, time-to-peak, reversal classification (`continuation | fade |
reversal`), and data completeness. Driven by synthetic sample paths today and
real intraday bars later — only the importer changes.

### Similarity scoring (`similarity_engine.py`)

A query feature vector is built from the current context (regime, DXY, 2Y/10Y,
oil, gold, VIX, S&P, USD/MXN momentum, dominant event type, news tags) and scored
against every stored reaction. Categorical features (regime, event type) match
exactly; numeric features use a Gaussian `exp(-(Δ/scale)²)`; news tags use
Jaccard overlap. The final score is a **configurable weighted average**
(`SIMILARITY_WEIGHTS`) over features present on both sides.

### Probability model (`historical_statistics.py`)

From the top matches we report aggregate stats (similarity-weighted average move,
median move, win rate vs the trade direction, p25–p75 expected range, expected
holding time, typical MFE/MAE). The probability forecast estimates, for the
current trade direction and price, the fraction of similar events whose favorable
excursion reached each level and whose adverse excursion hit the stop:

```
probability_reaches_target_1 / target_2 / stretch
probability_hits_stop
```

(Targets come from the live signal; when the signal is `NO_TRADE` there are no
levels, so these are `null`.)

### Blended, configurable confidence

`blend_confidence` combines five 0–100 components with configurable weights
(`CONFIDENCE_WEIGHTS`): current weighted **signal**, **historical** similarity
quality, **regime** confidence, **volatility** quality, and news/calendar **data
quality**. Missing components (e.g. no historical matches) are dropped and the
weights renormalized, so absent data never *lowers* confidence. The blended value
becomes the recommendation's `confidence`, with the full breakdown in
`confidence_breakdown`.

### Backfill workflow

Importers share one interface. Event importers implement `fetch_events` +
`fetch_price_path` (populating `historical_events`, `historical_event_reactions`,
and event-linked `historical_market_snapshots`); series importers implement
`fetch_series` (populating standalone `historical_market_snapshots`). The base
`run_all()` runs whichever the importer declares (`provides_events` /
`provides_series`).

`ensure_history_seeded(db)` keeps the system always populated: it auto-runs only
*lazy-safe* importers (`mock`/`csv` — local, no network) on demand when history
is empty, and otherwise seeds the mock sample so similarity always has data.
**Expensive provider backfill never runs on a page load** — use the CLI.

#### Run a real backfill (CLI)

```bash
# Uses HISTORY_IMPORTER (default: mock)
python -m app.scripts.backfill_history

# Real, no API key — drop CSVs in CSV_HISTORY_DIR
CSV_HISTORY_DIR=/path/to/csv python -m app.scripts.backfill_history --importer csv --reset

# Alpha Vantage (USD/MXN + WTI oil + S&P proxy daily history)
ALPHA_VANTAGE_API_KEY=... python -m app.scripts.backfill_history --importer alphavantage

# FRED (US 2Y/10Y yields, dollar-index proxy, VIX, WTI)
FRED_API_KEY=... python -m app.scripts.backfill_history --importer fred
```

`--reset` wipes the historical tables first (safe-guarded behind the flag).
API keys are read from the environment and **never printed or logged** (every
provider error is scrubbed). On Vercel the SQLite DB is ephemeral, so run
backfill against a persistent `DATABASE_URL` (Postgres) for durable history.

#### Importers + required keys

| `HISTORY_IMPORTER` | Data | Key | Populates |
|---|---|---|---|
| `mock` (default) | Synthetic sample | none | events + reactions |
| `csv` | Your CSV exports | none | events + reactions + series |
| `alphavantage` | USD/MXN, oil, S&P proxy (daily) | `ALPHA_VANTAGE_API_KEY` | series |
| `fred` | US2Y/US10Y, DXY proxy, VIX, oil | `FRED_API_KEY` | series |
| `yahoo` / `polygon` | (stubs) | — | — |

#### CSV format (`CSV_HISTORY_DIR`)

- `events.csv` (required): `event_key,event_type,event_name,country,release_time,
  forecast,actual,previous,importance,currency_impact,baseline,dxy,us2y,us10y,oil,
  gold,vix,sp_futures,momentum,regime,news_tags` (`release_time` ISO-8601,
  `news_tags` pipe-separated).
- `paths.csv` (optional): `event_key,hours,price` reaction paths.
- `series.csv` (optional): `series,ts,value` where `series` ∈ USDMXN, DXY, US2Y,
  US10Y, OIL, GOLD, VIX, SP_FUTURES.

#### Verify counts + sample vs real

```bash
curl -s localhost:8000/history/diagnostics | python -m json.tool
```

Returns the active importer, per-table counts (`historical_events`,
`historical_event_reactions`, `historical_market_snapshots`,
`similarity_matches`), the `data_class` (`sample` | `imported` | `live`), the
last-imported timestamp, and a **warning when only sample data is present**.
Similarity matching uses `historical_event_reactions`; once any real
(imported/live) reactions exist, sample reactions are excluded so matching runs
against real data only — the dashboard then shows "Historical Database" instead
of "Sample Historical Database".

### Limitations of the sample data

The sample dataset is a small, deterministic, **synthetic** reconstruction for
building and testing — its reaction paths are generated from each event's
surprise, not observed ticks. Treat similarity/probabilities as a *framework
demo* until a real provider is wired in. Free daily sources also can't support
true 15m/1h windows; those become meaningful only with intraday (paid) data.

## Project layout

```
AITradingAssistant/
  backend/
    app/
      main.py            # FastAPI app + dashboard
      config.py          # env-driven settings
      database.py        # SQLAlchemy engine/session (SQLite/Postgres, /tmp on serverless)
      models/            # MarketSnapshot, NewsItem, AnalysisSnapshot
      services/
        market_data.py   # USD/MXN + macro providers (mock | live | fallback)
        news.py          # news provider (mock + live stub)
        calendar.py      # economic calendar provider (mock + live stub)
        signals.py       # pure directional heuristics
        ai_analysis.py   # analyzer (rule-based default; OpenAI stub)
        context_builder.py  # context assembly + event timeline
      routers/
        health.py  market.py  analysis.py  news.py  calendar.py  timeline.py
    api/index.py         # Vercel serverless entrypoint
    vercel.json
    requirements.txt
    README.md
  docs/
    ROADMAP.md
```

## Setup

Requires Python 3.11+.

```bash
cd AITradingAssistant/backend

python3 -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate

pip install -r requirements.txt

# optional: configure environment
cp .env.example .env

uvicorn app.main:app --reload
```

Then open:

- Dashboard: http://127.0.0.1:8000/
- API docs (Swagger): http://127.0.0.1:8000/docs
- Health: http://127.0.0.1:8000/health

## Example responses

`GET /analysis/usdmxn` (abridged):

```json
{
  "direction": "BUY_USD",
  "trade_score": 71.2,
  "market_bias": "USD bullish",
  "confidence": 62.4,
  "momentum_status": "Bullish USD",
  "risk_level": "elevated",
  "usd_score": 24.1, "mxn_score": 8.0, "net_bias": 16.1,
  "market_regime": { "primary": "Fed Driven", "secondary": "Risk Off", "confidence": 41.2, "scores": { "Fed Driven": 1.4, "Risk Off": 1.0 }, "rationale": ["Fed event in focus: FOMC Rate Decision"] },
  "opportunity_grade": "B",
  "opportunity_grade_detail": { "grade": "B", "score": 64.8, "reasons": ["Signal agreement 50% ...", "Regime: Fed Driven (41.2% conf)."], "components": { "agreement": 0.5, "confidence": 0.624, "trade_score": 0.712, "risk_penalty": 8.0 } },
  "what_would_change_my_mind": ["USD/MXN trading below the stop at 17.86 would invalidate the BUY_USD view.", "If S&P Futures strengthens further (S&P fut 5467), the net bias weakens or flips."],
  "historical_similarity": { "status": "active", "best_similarity": 0.87, "sample_size": 8, "win_rate": 75.0, "average_move": 0.18, "median_move": 0.23 },
  "historical": { "best_similarity": 0.87, "sample_size": 8, "statistics": { "average_move": 0.18, "median_move": 0.23, "win_rate": 75.0, "expected_range": { "low_pct": 0.05, "high_pct": 0.41 }, "expected_duration": "3-5 days", "typical_MFE": 0.42, "typical_MAE": 0.0 }, "top_matches": [ { "event_type": "fed_rate_decision", "release_time": "2024-03-20T18:30:00+00:00", "similarity_score": 0.87, "windows": { "1d": 0.31 }, "reversal_behavior": "continuation" } ] },
  "probabilities": { "sample_size": 8, "levels": { "probability_reaches_target_1": 62.5, "probability_reaches_target_2": 50.0, "probability_reaches_stretch": 25.0, "probability_hits_stop": 12.5 }, "targets": { "target_1": 18.02, "stretch": 18.13, "stop": 17.86 } },
  "confidence_breakdown": { "value": 64.0, "components": { "signal": 62.4, "historical": 86.6, "regime": 41.2, "volatility": 79.6, "data_quality": 78.0 }, "weights_used": { "signal": 0.4, "historical": 0.25, "regime": 0.15, "volatility": 0.1, "data_quality": 0.1 } },
  "summary": "Bias favors USD strength vs MXN. Spot ~17.93 ... Confirming: DXY, US 10Y yield. Pushing back: S&P futures. ...",
  "key_drivers": ["DXY firmer (104.6)", "US 10Y yield up (4.38%)"],
  "market_drivers": [ { "name": "DXY", "value": 104.6, "lean": "USD+", "note": "US dollar index vs recent baseline" } ],
  "bullish_factors": ["DXY 104.6 → supports USD", "Data: US CPI (MoM) beat forecast (USD-positive)"],
  "bearish_factors": ["S&P futures 5467 → supports MXN"],
  "upcoming_risks": [ { "event": "US Retail Sales (MoM)", "importance": "high", "hours_away": 48.0, "note": "Could trigger volatility / invalidate the view" } ],
  "entry": 17.93,
  "target": 18.02, "stretch_target": 18.13, "stop": 17.86,
  "expected_move": "+0.50% (spot 17.93 -> 18.02)",
  "expected_duration": "1-2 days",
  "invalidation_level": 17.86,
  "risk_notes": "Mocked data in use; ... High-impact event(s) within 48h: ...",
  "timeline": [ { "type": "event", "label": "US CPI (MoM) released", "detail": "actual 0.4% vs forecast 0.3%" } ],
  "market": { "usdmxn": 17.93, "inverse_usdmxn": 0.05577, "dxy": 104.6, "us2y": 4.71, "us10y": 4.38, "oil": 75.1, "gold": 2381.2, "vix": 15.1, "provider": "mock", "source": "mock" },
  "context": { "upcoming_events": [], "released_events": [], "released_last_24h": [], "recent_news": [] }
}
```

## Configuration

All config is environment-driven (see `.env.example`):

| Variable | Purpose | Default |
| --- | --- | --- |
| `DATABASE_URL` | SQLite or Postgres connection string | `sqlite:///./aitrading.db` |
| `USE_MOCK_DATA` | Serve mocked data; set `false` to attempt live USD/MXN | `true` |
| `FX_API_KEY` | FX provider key/App ID for **live USD/MXN** | empty |
| `FX_PROVIDER` | FX provider name | `openexchangerates` |
| `FX_BASE_URL` | Override FX endpoint (optional) | OXR `latest.json` |
| `FRED_API_KEY` | FRED key for **live US 2Y / 10Y** treasury yields | empty |
| `ALPHA_VANTAGE_API_KEY` | Alpha Vantage key for **live oil / gold** (DXY/VIX/S&P fall back) | empty |
| `MACRO_CACHE_SECONDS` | Cache macro fetches to respect rate limits (AV ~25/day) | `600` |
| `REFRESH_POLICIES` | JSON of per-provider refresh minutes, e.g. `{"usdmxn":30,"news":10}` | built-in defaults |
| `MARKET_HOLIDAYS` | JSON list of ISO dates the FX market is closed | empty |
| `NEWS_API_KEY` | News provider key for **live news** (NewsAPI.org / Finnhub) | empty |
| `NEWS_PROVIDER` | Live news implementation (`newsapi` \| `finnhub` \| `fmp`) | `newsapi` |
| `NEWS_BASE_URL` | Override the news endpoint (optional) | NewsAPI `/v2/everything` |
| `CALENDAR_API_KEY` | Economic calendar provider key (Trading Economics) | empty |
| `CALENDAR_PROVIDER` | Live calendar implementation (`tradingeconomics` \| `finnhub` \| `csv`) | `tradingeconomics` |
| `CALENDAR_BASE_URL` | Override the calendar endpoint (optional) | Trading Economics |
| `CALENDAR_CSV_PATH` | Path to an importable calendar CSV (used when `CALENDAR_PROVIDER=csv`; no key needed) | empty |
| `HTTP_TIMEOUT_SECONDS` | HTTP timeout for provider calls | `8.0` |
| `SIGNAL_WEIGHTS` | JSON override of signal weights (see below) | unset (uses defaults) |
| `SIMILARITY_WEIGHTS` | JSON override of history similarity feature weights | unset (uses defaults) |
| `CONFIDENCE_WEIGHTS` | JSON override of blended-confidence weights | unset (uses defaults) |
| `HISTORY_IMPORTER` | Backfill source (`mock` \| `csv` \| `yahoo` \| `fred` \| `alphavantage` \| `polygon`) | `mock` |
| `CSV_HISTORY_DIR` | Directory with `events.csv` + `paths.csv` for the CSV importer | unset |
| `MARKET_DATA_API_KEY` | Alternate market feed (future) | empty |
| `FRED_API_KEY` | DXY / treasury yields (future) | empty |
| `OPENAI_API_KEY` / `AI_MODEL` | LLM-backed analysis (future) | empty |

### Live USD/MXN market data

Phase 1 can fetch a **real USD/MXN spot price** while keeping everything else
mocked (DXY, treasury yield, oil remain placeholders).

1. Get a free **Open Exchange Rates** App ID: https://openexchangerates.org/signup/free
2. In `.env`, set:

   ```bash
   USE_MOCK_DATA=false
   FX_API_KEY=your_app_id_here
   ```

3. Restart the server. `GET /market/usdmxn` now returns a live price.

The `source` field on every stored snapshot tells you where the data came from:

| `source` | Meaning |
| --- | --- |
| `mock` | `USE_MOCK_DATA=true` — intentional mock data |
| `live` | Real price fetched from the FX provider |
| `fallback` | Live was requested but the key was missing or the fetch failed; mock data used so the API never breaks |

Open Exchange Rates returns USD-based rates, so `USD/MXN = rates["MXN"]`. To use
a different provider, implement a new branch/class in
`services/market_data.py` and point `FX_BASE_URL` / `FX_PROVIDER` at it.

### Switching to Postgres

```bash
DATABASE_URL=postgresql+psycopg://user:password@localhost:5432/aitrading
```

(Install a driver, e.g. `pip install "psycopg[binary]"`.)

### Live news (NewsAPI.org)

1. Get a free key: https://newsapi.org/register
2. In `.env` set `USE_MOCK_DATA=false` and `NEWS_API_KEY=your_key`.
3. `GET /news/recent` now returns live, topic-filtered headlines; items are
   classified (affected currencies, importance, tags) and stored. Sentiment is a
   **placeholder** (defaults to neutral) until a real scorer lands.

### Live economic calendar (Trading Economics)

1. Get an API key: https://tradingeconomics.com/api/
2. In `.env` set `USE_MOCK_DATA=false` and `CALENDAR_API_KEY=your_key`.
3. `GET /calendar/upcoming` / `/calendar/released` now return live US + Mexico
   events mapped to the shared schema.

If either key is missing or a fetch fails, the service automatically falls back
to mock data so the API never breaks.

## Going live (plugging in real data)

The provider interfaces are designed so live integrations drop in without
touching routers or storage:

- `services/market_data.py` → `LiveMarketDataProvider` (USD/MXN) is **implemented**
  (Open Exchange Rates). Macro indicators are still mocked placeholders.
- `services/news.py` → `NewsAPIProvider` is **implemented**; `FinnhubNewsProvider`
  / `FMPNewsProvider` are stubs selectable via `NEWS_PROVIDER`.
- `services/calendar.py` → `TradingEconomicsCalendarProvider` is **implemented**;
  `FinnhubCalendarProvider` is a stub selectable via `CALENDAR_PROVIDER`.
- `services/ai_analysis.py` → implement `OpenAIAnalyzer.analyze()`.

Each orchestrator runs live-with-fallback, so the service never breaks if a
provider is down — it returns mock data (market is tagged `source="fallback"`).

## Deploy to Vercel

The backend ships with a Vercel config (`vercel.json` + `api/index.py`) that
serves the FastAPI app as a Python serverless function.

### One-time project setup

1. Create a new Vercel project from the `AITradingAssistant` GitHub repo.
2. **Set the project Root Directory to `backend`.** This is required so the
   `app` package imports correctly and `requirements.txt` is detected.
3. Add the environment variables below (Project → Settings → Environment
   Variables), then deploy.

### Required environment variables

| Variable | Value | Notes |
| --- | --- | --- |
| `USE_MOCK_DATA` | `false` | Enables live fetches |
| `FX_PROVIDER` | `openexchangerates` | FX provider name |
| `FX_API_KEY` | `<your Open Exchange Rates App ID>` | **Secret** — set in Vercel, never commit |

Optional live providers (omit to keep mock news/calendar):

| Variable | Value | Notes |
| --- | --- | --- |
| `NEWS_API_KEY` | `<NewsAPI.org or Finnhub key>` | **Secret** — enables live news |
| `NEWS_PROVIDER` | `finnhub` | Use Finnhub for live news |
| `FRED_API_KEY` | `<FRED key>` | **Secret** — enables live US 2Y / 10Y |
| `ALPHA_VANTAGE_API_KEY` | `<Alpha Vantage key>` | **Secret** — enables live oil / gold |
| `CALENDAR_API_KEY` | `<Trading Economics key>` | **Secret** — enables live calendar |

Optional: set `DATABASE_URL` to a Postgres URL for durable storage. By default
the app uses SQLite; on Vercel it writes to `/tmp/aitrading.db`, which is
**ephemeral per instance** (fine for Phase 1, snapshots are not shared across
cold starts). If `FX_API_KEY` is missing or invalid, the endpoints still work
and return `source: "fallback"` (mock data) — the service never breaks.

### Verify after deploy

```bash
curl https://<your-app>.vercel.app/health
curl https://<your-app>.vercel.app/market/usdmxn      # source: live (or fallback)
curl https://<your-app>.vercel.app/analysis/usdmxn
```

### How it works

- `api/index.py` exposes the ASGI `app` and calls `init_db()` at import, because
  Vercel's runtime may skip ASGI lifespan startup.
- `vercel.json` routes all paths (`/(.*)`) to the function so FastAPI sees the
  real request path.
- `app/database.py` redirects SQLite to `/tmp` when the `VERCEL` env var is set.

Local development is unchanged — none of this affects `uvicorn app.main:app`.

## Tests

A dependency-free smoke test covers all endpoints, the expanded analysis schema
(`market_drivers`, `bullish_factors`, `bearish_factors`, `upcoming_risks`,
`weighted_contributions`, `conflicting_signals`, `signal_breakdown`), the
reasoning engine (`market_regime` classification, `opportunity_grade` A+..PASS,
`what_would_change_my_mind`), the **historical intelligence engine**
(`/history/*` endpoints, reaction-window math, similarity ranking, directional
probability forecast, configurable confidence blend), the weighting engine
(defaults, env override, USD/MXN scoring, conflicts), the `mock` / `live` /
`fallback` source tagging for market **and** news **and** calendar, and asserts
that **API keys are scrubbed from error messages**:

```bash
cd backend
./.venv/bin/python -m tests.smoke_test   # or: python -m tests.smoke_test
```

## Next steps

See [`../docs/ROADMAP.md`](../docs/ROADMAP.md).
