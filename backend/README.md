# AI Trading Assistant — Backend (Phase 3.5)

Backend-only **USD/MXN market intelligence engine**. It collects market + macro
inputs, **live news**, and a **live economic calendar**, builds a structured
context, runs an **explainable reasoning engine** that classifies the market
regime, scores weighted USD-vs-MXN evidence, grades the opportunity, and explains
*why* it sees an edge (and what would change its mind), persists everything
(market snapshots, news, and full recommendation snapshots), and exposes a JSON
API plus a dashboard.

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
  secrets.py       scrubs API keys out of any log/error string
models/          MarketSnapshot, NewsItem, AnalysisSnapshot (SQLAlchemy)
```

Flow for `GET /analysis/usdmxn`: capture market snapshot + news (stored) →
`build_context` (DB news/analyses + calendar provider) → `build_timeline` →
`analyzer.analyze(market, news, calendar, recent_analyses)` → store
`AnalysisSnapshot` with context + timeline → return.

## Provider system & mock fallback

Each external dependency sits behind an interface + factory, selected by config:

| Service | Factory | Live when | Else |
| --- | --- | --- | --- |
| Market (USD/MXN) | `get_market_data()` | `USE_MOCK_DATA=false` **and** `FX_API_KEY` set | `mock`; `fallback` on error |
| News | `get_news_provider()` | `USE_MOCK_DATA=false` **and** `NEWS_API_KEY` set | mock; `fallback` on error |
| Calendar | `get_calendar_provider()` | `USE_MOCK_DATA=false` **and** `CALENDAR_API_KEY` set | mock; `fallback` on error |
| Analyzer | `get_analyzer()` | `USE_MOCK_DATA=false` **and** `OPENAI_API_KEY` set | rule-based |

Every live provider degrades safely: if a fetch fails or the key is missing,
the service returns mock data (tagged `source="fallback"` for market) and never
breaks. **API keys are never written to logs** — keys are sent via request
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
  "historical_similarity": { "status": "placeholder", "score": null, "sample_size": 4 },
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
| `NEWS_API_KEY` | News provider key for **live news** (NewsAPI.org) | empty |
| `NEWS_PROVIDER` | Live news implementation (`newsapi` \| `finnhub` \| `fmp`) | `newsapi` |
| `NEWS_BASE_URL` | Override the news endpoint (optional) | NewsAPI `/v2/everything` |
| `CALENDAR_API_KEY` | Economic calendar provider key (Trading Economics) | empty |
| `CALENDAR_PROVIDER` | Live calendar implementation (`tradingeconomics` \| `finnhub`) | `tradingeconomics` |
| `CALENDAR_BASE_URL` | Override the calendar endpoint (optional) | Trading Economics |
| `HTTP_TIMEOUT_SECONDS` | HTTP timeout for provider calls | `8.0` |
| `SIGNAL_WEIGHTS` | JSON override of signal weights (see below) | unset (uses defaults) |
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
| `NEWS_API_KEY` | `<NewsAPI.org key>` | **Secret** — enables live news |
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
`what_would_change_my_mind`), the weighting engine (defaults, env override,
USD/MXN scoring, conflicts), the `mock` / `live` / `fallback` source tagging for
market **and** news **and** calendar, and asserts that **API keys are scrubbed
from error messages**:

```bash
cd backend
./.venv/bin/python -m tests.smoke_test   # or: python -m tests.smoke_test
```

## Next steps

See [`../docs/ROADMAP.md`](../docs/ROADMAP.md).
