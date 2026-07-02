# Centroid/GFC FIX 4.4 — Phase 1 (Market Data, Read-Only)

Phase 1 connects to the **GFC/Centroid demo FIX market-data session**, subscribes to
USD/MXN (or a configured symbol), and displays **executable bid/ask** on the dashboard.
**No trading messages are sent** — no `NewOrderSingle`, no auto-hedging, no live order buttons.

Code: `backend/app/services/fix/`

---

## Architecture

| Layer | Role |
|---|---|
| Raw FIX TCP/SSL session | `centroid_md_session.py` — logon, heartbeat, MD subscribe |
| Message codec | `codec.py`, `messages.py`, `parser.py` |
| Quote cache | `quote_store.py` — in-memory latest bid/ask + session health |
| Simulation stubs | `simulation.py` — `SimulatedOrder` objects (never transmitted) |
| Diagnostics | `GET /diagnostics/fix` — secrets redacted |

Existing AI recommendations **continue unchanged** if FIX is unavailable.

---

## Required environment variables (market data)

| Variable | Purpose |
|---|---|
| `CENTROID_MD_ENABLED` | `true` to start background MD session at app startup |
| `CENTROID_MD_HOST` | FIX MD host |
| `CENTROID_MD_PORT` | FIX MD port (often price session, e.g. 5001) |
| `CENTROID_MD_USERNAME` | Logon username (Tag 553) |
| `CENTROID_MD_PASSWORD` | Logon password (Tag 554) — **never logged or returned in API** |
| `CENTROID_MD_SENDER_COMP_ID` | Tag 49 |
| `CENTROID_MD_TARGET_COMP_ID` | Tag 56 |
| `CENTROID_MD_SSL` | `true` / `false` |
| `CENTROID_MD_RESET_ON_LOGON` | `true` resets outbound seq on logon (Tag 141=Y) |
| `CENTROID_MD_SYMBOL_USDMXN` | Symbol for MD request (default `USD/MXN`) |

## Future trading session (Phase 2+ — unused)

`CENTROID_TD_HOST`, `CENTROID_TD_PORT`, `CENTROID_TD_USERNAME`, `CENTROID_TD_PASSWORD`,
`CENTROID_TD_SENDER_COMP_ID`, `CENTROID_TD_TARGET_COMP_ID`, `CENTROID_TD_ACCOUNT`,
`CENTROID_TD_SSL`, `CENTROID_TD_RESET_ON_LOGON`

---

## Deployment: persistent worker required

**Live FIX quotes will not work from Vercel serverless alone.**

FIX 4.4 market data is a long-lived TCP (or TCP+SSL) session with heartbeats and sequence
numbers. Vercel serverless functions are ephemeral — they cold-start, handle a request, and
exit. They cannot maintain a FIX socket across invocations.

To receive live bid/ask updates you need a **persistent worker or always-on host**, for example:

- A VM or container running `uvicorn app.main:app` with `CENTROID_MD_ENABLED=true`
- Railway, Fly.io, or similar always-on process
- A dedicated FIX sidecar worker that maintains the session and writes quotes to Postgres
  (future enhancement; Phase 1 uses in-memory quotes on the worker process)

The Vercel deployment can still serve the dashboard and `GET /diagnostics/fix`, but without a
persistent FIX worker the **Centroid FIX Market Data** card will show `disconnected` or
`not_configured` — AI recommendations continue to work from other providers.

### Configure credentials (any host)

1. Add each `CENTROID_MD_*` variable from your **GFC demo credential pack** (never commit values).
2. Set `CENTROID_MD_ENABLED=true` on the **persistent worker only** (not on ephemeral serverless).
3. Restart/redeploy the worker after env changes.

You may store the same env vars in Vercel for documentation or a future Postgres-backed quote
relay, but setting them on Vercel **does not** establish a live FIX session by itself.

---

## Dashboard

**Centroid FIX Market Data** card shows:

- Status (connected / disconnected / error / not configured)
- Symbol, Bid, Ask, Spread
- Last update + session heartbeat health

---

## Diagnostics API

```http
GET /diagnostics/fix
```

Returns session status, quote, and credential *presence* flags (not values).

---

## Phase 1 guarantees

- ✅ FIX 4.4 logon, heartbeat, test request response, logout, sequence numbers
- ✅ MarketDataRequest subscribe + snapshot/incremental parsing
- ✅ Simulation-only order objects
- ❌ No `NewOrderSingle`
- ❌ No live execution
- ❌ No auto-hedging
- ❌ No live order buttons

---

## Related

- [`PROVIDERS.md`](../../Border-Currency-Shipments/ai-trading-assistant/PROVIDERS.md) — provider registry (section 6)
- [`MODEL_HISTORY.md`](../../Border-Currency-Shipments/ai-trading-assistant/MODEL_HISTORY.md) — Phase 6 FIX entry
