# AI Trading Assistant — Backend (Phase 1)

Backend-only **USD/MXN market intelligence assistant**. It collects market +
macro inputs (mocked for now), runs an analysis engine, persists snapshots, and
exposes a small JSON API plus an optional dashboard.

> Phase 1 scope: **no iPhone app, no SaaS billing, no auto-trading.** This is a
> read-only intelligence service that produces a directional view, not orders.

## Features

- FastAPI service with three core endpoints:
  - `GET /health` — service status.
  - `GET /market/usdmxn` — current USD/MXN snapshot + macro drivers (stored).
  - `GET /analysis/usdmxn` — AI analysis of the latest snapshot (stored).
- Persists **market snapshots** and **AI analysis snapshots** (SQLite by
  default; Postgres-ready via `DATABASE_URL`).
- Tracks: USD/MXN price, **DXY**, **US Treasury yield**, **Oil**, **News**, and
  **Economic calendar** (placeholders, all mockable).
- Modular provider layer — drop in real data/LLM providers without touching the
  routers.
- Optional HTML dashboard at `/`.

## Project layout

```
AITradingAssistant/
  backend/
    app/
      main.py            # FastAPI app + optional dashboard
      config.py          # env-driven settings
      database.py        # SQLAlchemy engine/session (SQLite/Postgres)
      models/            # MarketSnapshot, AnalysisSnapshot
      services/
        market_data.py   # USD/MXN + macro providers (mock + live stub)
        news.py          # news + economic calendar providers (mock + live stub)
        signals.py       # pure directional heuristics
        ai_analysis.py   # analyzer (rule-based default; OpenAI stub)
      routers/
        health.py
        market.py
        analysis.py
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

`GET /analysis/usdmxn`:

```json
{
  "direction": "BUY_USD",
  "confidence": 62.4,
  "summary": "Bias favors USD strength vs MXN. Spot ~17.93 ...",
  "key_drivers": ["DXY firmer (104.6)", "US 10Y yield up (4.38%)"],
  "target": 18.02,
  "stretch_target": 18.13,
  "stop": 17.86,
  "momentum_status": "Bullish USD",
  "risk_notes": "Mocked data in use; not investment advice. ...",
  "model": "mock-rules-v1",
  "market": { "usdmxn": 17.93, "dxy": 104.6, "treasury_yield": 4.38, "oil": 75.1 }
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
| `HTTP_TIMEOUT_SECONDS` | HTTP timeout for provider calls | `8.0` |
| `MARKET_DATA_API_KEY` | Alternate market feed (future) | empty |
| `FRED_API_KEY` | DXY / treasury yields (future) | empty |
| `NEWS_API_KEY` | News + calendar (future) | empty |
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

## Going live (plugging in real data)

The provider interfaces are designed so live integrations drop in without
touching routers or storage:

- `services/market_data.py` → `LiveMarketDataProvider` (USD/MXN) is **implemented**
  (Open Exchange Rates). Macro indicators are still mocked placeholders.
- `services/news.py` → implement `LiveNewsProvider`.
- `services/ai_analysis.py` → implement `OpenAIAnalyzer.analyze()`.

`get_market_data()` orchestrates live-with-fallback, so the service never breaks
if a provider is down — it returns mock data tagged `source="fallback"`.

## Tests

A dependency-free smoke test covers `/health`, `/market/usdmxn`,
`/analysis/usdmxn`, and the `mock` / `live` / `fallback` source tagging:

```bash
cd backend
./.venv/bin/python -m tests.smoke_test   # or: python -m tests.smoke_test
```

## Next steps

See [`../docs/ROADMAP.md`](../docs/ROADMAP.md).
