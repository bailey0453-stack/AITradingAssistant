# AI Trading Assistant

A USD/MXN market intelligence engine (backend-only). It collects market + macro
inputs, news, and an economic calendar, builds context, runs an analysis engine,
persists market/news/recommendation snapshots, and serves a JSON API plus a
dashboard.

Endpoints: `/health`, `/market/usdmxn`, `/analysis/usdmxn`, `/news/recent`,
`/calendar/upcoming`, `/timeline/usdmxn`.

Not in scope: iPhone app, SaaS billing, auto-trading.

## Repository layout

- [`backend/`](backend/) — FastAPI service. See [`backend/README.md`](backend/README.md) for setup.
- [`docs/ROADMAP.md`](docs/ROADMAP.md) — phased plan.

## Quick start

```bash
cd backend
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
uvicorn app.main:app --reload
```

Open http://127.0.0.1:8000/ (dashboard) or http://127.0.0.1:8000/docs (API).

## Deploy

The backend deploys to Vercel as a Python serverless function. Set the Vercel
project **Root Directory to `backend`** and configure the env vars documented in
[`backend/README.md`](backend/README.md#deploy-to-vercel) (`USE_MOCK_DATA`,
`FX_PROVIDER`, `FX_API_KEY`).
