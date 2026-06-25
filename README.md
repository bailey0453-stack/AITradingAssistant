# AI Trading Assistant

A USD/MXN market intelligence assistant. **Phase 1 is backend-only**: collect
market + macro inputs, run an analysis engine, persist snapshots, and serve a
small JSON API (plus an optional dashboard).

Not in Phase 1: iPhone app, SaaS billing, auto-trading.

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
