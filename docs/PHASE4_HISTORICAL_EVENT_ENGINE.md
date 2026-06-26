# Phase 4 — Historical Event Engine (Design)

> **Status: IMPLEMENTED (sample data).** The engine shipped in
> `app/services/history/` with a working mock/sample importer; this document is
> the original design. A few names changed in the build — see the note below.

> **Implementation notes (what actually shipped):**
> - Tables use explicit names instead of the `hist_*` prefix:
>   `historical_market_snapshots`, `historical_events`,
>   `historical_event_reactions`, and a derived `similarity_matches` cache.
> - Code lives in `app/services/history/`: `importers.py` (modular backfill with
>   a working `MockSampleImporter` + CSV/Yahoo/FRED/Alpha Vantage/Polygon stubs),
>   `historical_prices.py` (reaction-window math), `historical_events.py`
>   (DB access + idempotent seeding), `similarity_engine.py` (feature vectors +
>   weighted scoring), `historical_statistics.py` (aggregate stats, probability
>   forecast, configurable confidence blend).
> - Endpoints: `GET /history/events`, `/history/similar`, `/history/statistics`,
>   `/history/probabilities`; the history block also feeds `/analysis/usdmxn`.
> - Reaction windows shipped as **15m/1h/4h/1d/3d/5d** (added 5d).
> - Weights are configurable via `SIMILARITY_WEIGHTS` and `CONFIDENCE_WEIGHTS`.
>
> The original design below is retained for context and for the future real-data
> backfill.

> **Original goal.** This document specifies the
> tables, provider interfaces, backfill workflow, reaction math, and similarity
> scoring so we can **backfill years of history immediately** instead of waiting
> for the live service to accumulate data organically.

## 1. Goal & principles

Turn the assistant from "what is happening now" into "what usually happens after
events like this." We import historical market data and economic events, measure
how USD/MXN reacted in fixed windows after each event, and make those reactions
searchable ("find events like this one").

Design principles (carried over from Phases 1–3):

- **Modular providers, no vendor lock-in.** Every data source sits behind an
  interface with a mock/sample fallback. Missing API key ⇒ sample import, never a
  crash.
- **Clear provenance.** Every imported row records its `source` and
  `source_quality` so we never confuse a vendor's intraday tick with a
  reconstructed daily proxy.
- **Public vs proprietary separation.** Public/free historical backfill lives in
  **new tables**; the proprietary live recommendation history (`AnalysisSnapshot`
  from Phase 3) is never mixed in (see §8).
- **Reproducible.** Backfill is an idempotent, re-runnable job keyed by
  `(event_type, release_time)` and `(series, timestamp)` so re-imports upsert
  rather than duplicate.

### What we backfill

| Category | Series / events |
| --- | --- |
| Market price | USD/MXN |
| Macro market | DXY, US 2Y yield, US 10Y yield, WTI oil, VIX |
| US events | CPI, PPI, NFP/jobs, Fed rate decisions (FOMC) |
| Mexico events | Banxico rate decisions, Mexico CPI |
| Headlines | Major tariff/trade headlines (best-effort, where available) |

---

## 2. Database tables

New module `app/models/history.py` (separate `Base` metadata is **not** needed —
same SQLAlchemy `Base`, but a distinct table namespace prefixed `hist_`). All
timestamps are stored UTC ISO‑8601. JSON columns map to TEXT on SQLite and
JSON/JSONB on Postgres (consistent with Phase 1–3 models).

### 2.1 `hist_price_bars` — historical market time series

One row per (series, timestamp, granularity). Powers reaction-window math.

| Column | Type | Notes |
| --- | --- | --- |
| `id` | int PK | |
| `series` | str | `USDMXN` \| `DXY` \| `US2Y` \| `US10Y` \| `WTI` \| `VIX` |
| `ts` | datetime (idx) | bar open time, UTC |
| `granularity` | str | `1m` \| `5m` \| `1h` \| `1d` |
| `open/high/low/close` | float | OHLC; for yields/index `close` is the level |
| `volume` | float null | when available |
| `provider` | str | e.g. `stooq`, `yfinance`, `fred`, `sample` |
| `source_quality` | str | `tick` \| `intraday` \| `daily` \| `proxy` |
| unique | (`series`,`ts`,`granularity`) | idempotent upsert key |

> Intraday USD/MXN (for the 15m/1h/4h windows) is the hardest free data to get
> historically (see §7). Where only daily data exists, `source_quality="daily"`
> and the sub-daily windows are recorded as `null` rather than faked.

### 2.2 `hist_events` — historical economic calendar events

| Column | Type | Notes |
| --- | --- | --- |
| `id` | int PK | |
| `event_type` | str (idx) | normalized: `US_CPI`, `US_PPI`, `US_NFP`, `FED_RATE`, `BANXICO_RATE`, `MX_CPI`, `US_GDP`, `MX_GDP`, `TARIFF_HEADLINE` |
| `country` | str | `US` \| `MX` |
| `release_time` | datetime (idx) | scheduled/actual release, UTC |
| `forecast` | float null | consensus, normalized numeric |
| `actual` | float null | |
| `previous` | float null | |
| `surprise` | float null | `actual − forecast` (see §5.1) |
| `surprise_z` | float null | standardized surprise (z-score across history of that `event_type`) |
| `unit` | str | `pct`, `bps`, `k_jobs`, `level` — needed to compare like-for-like |
| `importance` | str | `high` \| `medium` \| `low` |
| `currency_impact` | str | `USD` \| `MXN` |
| `headline` | text null | for `TARIFF_HEADLINE` / context |
| `provider` | str | `fred`, `te`, `sample`, … |
| `source_quality` | str | `official` \| `vendor` \| `reconstructed` |
| unique | (`event_type`,`release_time`) | idempotent upsert key |

### 2.3 `hist_event_reactions` — measured USD/MXN reaction per event

One row per event (1:1 with `hist_events`), holding the computed windows and
summary stats. Splitting this from `hist_events` lets us recompute reactions
(e.g. after importing better intraday data) without touching event facts.

| Column | Type | Notes |
| --- | --- | --- |
| `id` | int PK | |
| `event_id` | FK → `hist_events.id` (unique) | |
| `baseline_usdmxn` | float null | USD/MXN at/just before `release_time` |
| `pre_window` | JSON | market conditions **before** event (see §2.4) |
| `usdmxn_move` | JSON | `{ "15m": Δ%, "1h": …, "4h": …, "1d": …, "3d": … }` |
| `dxy_move` | JSON | same windows |
| `yield_move` | JSON | `{ "us2y": {…windows}, "us10y": {…windows} }` |
| `oil_move` | JSON | same windows |
| `vix_move` | JSON | same windows |
| `max_favorable` | float | max USD-strengthening move within 3d (signed by event lean) |
| `max_adverse` | float | max counter move within 3d |
| `time_to_peak_min` | int null | minutes from release to peak favorable move |
| `reversal` | JSON | `{ "reversed": bool, "reversal_window": "4h", "retrace_pct": … }` |
| `data_completeness` | str | `full` \| `daily_only` \| `partial` — which windows are real |
| `computed_at` | datetime | |

### 2.4 `pre_window` JSON shape (market conditions before the event)

```json
{
  "lookback_hours": 24,
  "usdmxn": 17.91, "usdmxn_trend_pct": -0.3,
  "dxy": 104.4, "dxy_trend_pct": +0.2,
  "us2y": 4.71, "us10y": 4.36, "yield_trend_bps_10y": +4,
  "wti": 76.1, "wti_trend_pct": -1.1,
  "vix": 15.2, "vix_regime": "low"   // low <16, elevated 16–20, high >20
}
```

### 2.5 `hist_event_features` — denormalized vector for similarity (optional but recommended)

Materialized search features so similarity queries don't recompute from JSON.
Recomputable from §2.2–2.4.

| Column | Type | Notes |
| --- | --- | --- |
| `event_id` | FK (unique) | |
| `event_type` | str (idx) | |
| `surprise_z` | float | standardized surprise |
| `dxy_trend_pct` | float | pre-event DXY trend |
| `yield_trend_bps` | float | pre-event 10Y trend |
| `oil_trend_pct` | float | |
| `vix_level` | float | |
| `vix_regime` | str (idx) | bucket for fast filtering |
| `usdmxn_trend_pct` | float | |
| `outcome_1d_pct` | float null | label for later modeling/backtests |

### 2.6 Relationship to existing tables

- **Unchanged & isolated:** `analysis_snapshots` (Phase 3 proprietary live
  recommendations) — never written by backfill.
- The live engine *reads* `hist_*` at inference time for similarity, but the
  historical importer never writes to the live recommendation tables. See §8.

---

## 3. Provider interfaces

New package `app/services/history/` with small, swappable interfaces mirroring
the Phase 3 provider pattern (interface + concrete impls + `Sample*` fallback +
factory selected by config).

### 3.1 Price history provider

```python
class PriceHistoryProvider(ABC):
    source = "base"
    def get_bars(self, series: str, start: datetime, end: datetime,
                 granularity: str) -> list[PriceBar]: ...
    def supports(self, series: str, granularity: str) -> bool: ...
```

- `series` ∈ {USDMXN, DXY, US2Y, US10Y, WTI, VIX}.
- Implementations advertise capability via `supports()` so the orchestrator can
  pick the best provider per (series, granularity) and fall back gracefully.
- Concrete impls (Phase 4 build order): `StooqPriceProvider` (free daily),
  `YFinancePriceProvider` (free daily + limited intraday), `FredPriceProvider`
  (free daily for yields/DXY/oil via series IDs), `SamplePriceProvider`
  (bundled CSV fixtures used when no network/keys). Paid: `PolygonPriceProvider`,
  `TwelveDataPriceProvider`, `DukascopyFxProvider` (true historical intraday FX).

### 3.2 Economic event provider

```python
class EventHistoryProvider(ABC):
    source = "base"
    def get_events(self, event_types: list[str],
                   start: date, end: date) -> list[HistEvent]: ...
```

- Concrete: `FredReleasesProvider` (CPI/PPI/NFP/GDP actuals + release dates, free),
  `SampleEventProvider` (curated fixtures). Paid/keyed: `TradingEconomicsEvents`,
  `FinnhubEconomicCalendar` (give forecast + actual + timestamps in one call).
- Normalization (`event_type`, `unit`, surprise) is provider-independent and
  lives in a shared `normalize.py`, so vendors are interchangeable.

### 3.3 Headline provider (tariff/trade, best-effort)

```python
class HeadlineHistoryProvider(ABC):
    def get_headlines(self, query: str, start: date, end: date) -> list[HistHeadline]: ...
```

- Free historical news is weak (see §7). Default `SampleHeadlineProvider` ships a
  small curated set of major tariff/trade dates; `GdeltHeadlineProvider` (free,
  global news index) is the first real impl; paid options noted in §9.

### 3.4 Factory & config

```
HISTORY_PRICE_PROVIDER=stooq|yfinance|fred|polygon|twelvedata|sample
HISTORY_EVENT_PROVIDER=fred|tradingeconomics|finnhub|sample
HISTORY_HEADLINE_PROVIDER=gdelt|sample
HISTORY_START=2015-01-01          # default backfill horizon
USE_MOCK_DATA / *_API_KEY          # missing key ⇒ Sample* provider, source_quality="proxy"/"reconstructed"
```

Factories follow Phase 3: if `USE_MOCK_DATA=true` or no key ⇒ `Sample*`; else the
selected provider with **resilient fallback** to `Sample*` on error (logged via
`services/secrets.py` scrubbing).

---

## 4. Backfill workflow

A CLI/management entrypoint (e.g. `python -m app.history.backfill`) plus an
internal admin route guarded behind config (not a public endpoint). Steps:

1. **Plan.** Resolve horizon `[HISTORY_START, today]` and the series/event list.
2. **Import price series** (per series, chunked by date range) → upsert
   `hist_price_bars`. Pick provider per (series, granularity) via `supports()`;
   record `provider` + `source_quality`.
3. **Import events** → normalize (`event_type`, `unit`, numeric forecast/actual)
   → compute `surprise` and `surprise_z` (z over that event_type's history) →
   upsert `hist_events`.
4. **Import headlines** (best-effort) → tag tariff/trade → store as
   `TARIFF_HEADLINE` events with `headline` populated.
5. **Compute reactions** (§5) for every event that has sufficient surrounding
   price data → upsert `hist_event_reactions` and `hist_event_features`.
6. **Report.** Print/import-log a summary: rows imported, coverage %, windows
   that are `daily_only`, provider mix, gaps.

Properties: **idempotent** (unique keys → upsert), **resumable** (per-series /
per-year checkpoints), **incremental** (a daily cron can append the latest day),
and **dry-run** capable. Reaction computation is a *separate* re-runnable step so
we can recompute after upgrading price granularity without re-importing events.

---

## 5. Reaction calculation method

### 5.1 Surprise

- Numeric `forecast`/`actual` normalized to a common `unit` per `event_type`.
- `surprise = actual − forecast` (for rate decisions, in bps; for CPI/PPI, in
  pct points; for NFP, in thousands of jobs).
- `surprise_z = (surprise − mean) / std` over that event_type's historical
  surprises → lets us compare "how big a beat" across different event types.
- Direction lean reuses the Phase 3 convention (`event_surprise()` in
  `signal_weights.py`): a US upside beat ⇒ USD+, etc.

### 5.2 Reaction windows

For each event at `release_time t0`, and each window
`w ∈ {15m, 1h, 4h, 1d, 3d}`:

- `baseline = price(series, at=t0)` using the last bar at/just before `t0`.
- `move_w% = (price(series, at=t0+w) / baseline − 1) × 100` for USDMXN/DXY/WTI;
  yields recorded as **bps change** (`(level(t0+w) − level(t0)) × 100`).
- All series (USD/MXN, DXY, 2Y, 10Y, oil, VIX) get the same windows so reactions
  are directly comparable.
- **Sign convention:** USD/MXN moves are stored raw (+ = USD strengthening). A
  derived `usdmxn_move_aligned` flips sign by the event's expected lean so
  "favorable" always means "in the direction the event implied."

### 5.3 Summary statistics (within the 3-day envelope, finest available granularity)

- `max_favorable` = largest aligned favorable excursion.
- `max_adverse` = largest aligned adverse excursion (the "heat" before it worked).
- `time_to_peak_min` = minutes from `t0` to the favorable peak.
- `reversal` = did price give back ≥ X% of the peak within the window?
  `{reversed, reversal_window, retrace_pct}` (X configurable, default 50%).
- `data_completeness`: `full` if intraday bars exist for all windows;
  `daily_only` if only EOD (then 15m/1h/4h are `null`, 1d/3d computed from daily);
  `partial` otherwise. **We never fabricate intraday from daily.**

### 5.4 Timezone & session handling

- All math in UTC. Events released when FX is thin (weekends/holidays) snap the
  baseline to the next available bar and flag `pre_window.thin_liquidity=true`.
- US data releases at 8:30 ET, FOMC 14:00 ET, Banxico per schedule — the importer
  stores the real `release_time`, not the calendar date, so 15m/1h windows are
  meaningful.

---

## 6. Similarity scoring method ("find events like this one")

Given a query event (live or historical), rank historical events by similarity.

### 6.1 Candidate filter (cheap, indexed)

- Same or related `event_type` (configurable: exact, or a group like
  "US inflation" = {US_CPI, US_PPI}).
- Optional `vix_regime` match and date range.

### 6.2 Feature vector (from `hist_event_features`)

Normalized (z-scored) components, each weighted (reusing the Phase 3 idea of a
**configurable weight table**, e.g. `HISTORY_SIMILARITY_WEIGHTS`):

| Feature | Default weight |
| --- | --- |
| event_type match (categorical) | hard filter + 0.30 |
| surprise_z distance | 0.25 |
| DXY pre-trend distance | 0.15 |
| yield (10Y) pre-trend distance | 0.10 |
| oil pre-trend distance | 0.08 |
| VIX level/regime distance | 0.07 |
| USD/MXN pre-trend distance | 0.05 |

### 6.3 Scoring

- `distance = Σ wᵢ · |zᵢ(query) − zᵢ(candidate)|` (weighted Manhattan; Euclidean
  optional). `similarity = 1 / (1 + distance)` → 0..1.
- Return top-N with each match's reaction summary, plus an **aggregate**:
  median/mean USD/MXN move per window, hit-rate (share that moved in the lean
  direction by 1d), average `max_adverse` (typical heat), and reversal frequency.
- This aggregate is what the live analyzer cites as real
  `historical_similarity` (replacing the Phase 3 placeholder), e.g. *"23 similar
  US CPI upside surprises: USD/MXN +0.4% median by 1d, 68% hit-rate, avg adverse
  −0.2%, reversed 30% of the time."*

### 6.4 Exposure

- Internal service `services/history/similarity.py` + a **read-only** endpoint
  `GET /history/similar?event_type=US_CPI&surprise=0.2&...` (additive; does not
  change existing endpoints). The live `/analysis/usdmxn` enriches its response
  by calling this internally.

---

## 7. Limitations of historical data quality

- **Free intraday FX history is the weak point.** Stooq/yfinance/FRED give solid
  **daily** USD/MXN, DXY, yields, oil, VIX, but reliable **15m/1h/4h** USD/MXN
  going back years generally requires a paid feed (Dukascopy is a notable free-ish
  exception with caveats). Expect many older events to be `daily_only`.
- **Forecast/consensus history** is inconsistent in free sources. FRED has
  *actuals* and release dates but not always the *consensus forecast*; without
  forecast we cannot compute `surprise`. Paid calendars (Trading Economics,
  Finnhub) provide forecast+actual+timestamp together.
- **Release timestamps:** some free event data is dated to the day, not the
  minute → sub-hour windows become unreliable; flagged via `source_quality` and
  `data_completeness`.
- **Survivorship / revisions:** economic figures get revised; we store the
  first-print actual where possible and flag revisions; price data may have gaps
  around holidays/halts.
- **Headlines:** comprehensive historical tariff/trade headlines with timestamps
  are hard for free; GDELT helps but is noisy. Treated as best-effort context,
  not a primary signal.
- **Regime drift:** 2015-era reactions may not generalize to today (different Fed
  regime, MXN carry dynamics). Similarity is descriptive, **not** predictive — we
  surface base rates with sample sizes, never guarantees.

All limitations are made explicit in-row (`source_quality`, `data_completeness`)
and in the similarity output (sample size, completeness mix) so consumers can
weight conclusions appropriately.

---

## 8. Public backfill vs proprietary live history (separation)

- **Public/free historical backfill** → `hist_*` tables. Reproducible from public
  sources; could be shared/open without leaking IP.
- **Proprietary live recommendation history** → existing `analysis_snapshots`
  (Phase 3). Contains *our* weighted signals, recommendations, and outcomes — the
  real moat. **Never written by the backfill job.**
- **Boundary:** the live engine may *read* `hist_*` to compute similarity, but the
  importer has no access to and never writes `analysis_snapshots`. Optionally
  enforce with a separate `DATABASE_URL_HISTORY` (a second engine/DB) so public
  backfill can live in its own database entirely. Documented as a config switch;
  default keeps both in one DB with table-name separation.

---

## 9. Data sources: free vs paid

### Free / public (build against these first)

| Source | Covers | Notes |
| --- | --- | --- |
| **Stooq** | USD/MXN, DXY, WTI, indices — **daily** | CSV, no key, easy |
| **yfinance** (Yahoo) | FX, ^VIX, ^TNX/^IRX (yields proxy), CL=F oil — daily + *limited* recent intraday | unofficial; rate-limited |
| **FRED** (St. Louis Fed) | DGS2/DGS10 yields, DTWEXBGS (USD index), DCOILWTICO oil, VIXCLS, **CPI/PPI/NFP/GDP actuals + release dates** | free key; daily; authoritative for US macro actuals |
| **GDELT** | historical news/tariff headlines | free, noisy, best-effort |
| **Banxico SIE** | Mexico rates, Mexico CPI (INPC) | free key; official MX data |

### Paid / keyed (recommended when free is insufficient)

| Source | Why upgrade | Approx. role |
| --- | --- | --- |
| **Dukascopy / HistData** | true historical **intraday FX** (USD/MXN tick/1m) | enables real 15m/1h/4h windows |
| **Polygon.io** | intraday FX + indices, clean API | primary paid price feed |
| **Twelve Data** | FX/indices intraday, generous tiers | alternative price feed |
| **Trading Economics API** | calendar with **forecast + actual + timestamp** + MX coverage | best single event source |
| **Finnhub** | economic calendar + news, one key | alternative event/news feed |

**Recommendation:** Phase 4 v1 builds on **FRED (events + macro daily) + Stooq/
yfinance (price daily) + Sample fixtures**, accepting `daily_only` reactions for
older events. To unlock intraday reaction windows (15m/1h/4h) at quality, add
**Trading Economics** (events/forecasts) + **Polygon or Dukascopy** (intraday FX)
— both behind the existing provider interfaces, no engine rewrite.

---

## 10. Build order (when implementation is approved)

1. `app/models/history.py` (tables §2) + migration/`create_all`.
2. `app/services/history/` interfaces + `Sample*` providers + fixtures.
3. FRED + Stooq/yfinance providers (free) behind the interfaces.
4. `app/history/backfill.py` workflow (§4), idempotent + dry-run.
5. Reaction computation (§5) + `hist_event_reactions`/`hist_event_features`.
6. `services/history/similarity.py` (§6) + read-only `GET /history/similar`.
7. Wire real `historical_similarity` into `/analysis/usdmxn` (additive).
8. Smoke tests: sample backfill end-to-end, reaction math on fixtures, similarity
   ranking, "no key ⇒ sample" fallback, public/proprietary separation.

> No endpoints change for existing consumers; all Phase 4 additions are additive
> and read-only, consistent with Phases 1–3.
