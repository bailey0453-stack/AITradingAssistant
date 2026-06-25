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

## Phase 2 — Live data

- [ ] Implement `LiveMarketDataProvider` (USD/MXN feed; FRED for DXY + US 10Y; oil feed).
- [ ] Implement `LiveNewsProvider` (news API + economic calendar feed).
- [ ] Scheduled background polling to build a real time series.
- [ ] Caching + rate-limit handling for providers.

## Phase 3 — Smarter analysis

- [ ] Technical features (moving averages, ATR, RSI) over the stored time series.
- [ ] LLM-backed narrative analyzer (`OpenAIAnalyzer`) on top of rule-based guardrails.
- [ ] Backtesting harness against stored snapshots.
- [ ] Confidence calibration.

## Phase 4 — Delivery & access

- [ ] Auth (API keys / sessions).
- [ ] Alerts (email / push / WhatsApp) on signal changes.
- [ ] Public dashboard hardening.

## Later (explicitly out of Phase 1)

- iPhone app.
- SaaS billing / multi-tenant.
- Automated trade execution (would require strict risk controls + broker integration).
