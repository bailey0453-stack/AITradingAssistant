# GFC / Centroid FIX Conformance Plan

Source: `Centroid-FIX-ConformanceTest.numbers` supplied by GFC plus Centroid FIX 4.4 Trading API Specification v0.16.9.

## Safety boundary

- Demo/conformance only.
- `CENTROID_TD_ENABLED` defaults to `false`.
- `CENTROID_TD_CONFORMANCE_MODE` defaults to `true`, but cannot send unless trading is explicitly enabled.
- All order-control HTTP routes require admin authentication.
- Vercel never owns a trading TCP session; it proxies protected controls to the persistent Railway FIX worker.
- GFC credentials remain on Railway.
- Trading Logon never sets `ResetSeqNumFlag=Y`.
- No automatic model-driven order submission is implemented.

## Session / admin tests

| Workbook item | FIX type | Implementation | Status before live conformance |
|---|---|---|---|
| Logon | A | shared `build_logon`, dedicated trading session | ready for demo credentials |
| Heartbeat | 0 | shared heartbeat builder; 30s timer | ready |
| Test Request | 1 | inbound Test Request answered with matching heartbeat | ready |
| Logout | 5 | shared logout builder | ready |
| Resend Request | 2 | not yet exposed as an operator-triggered conformance action | pending |
| Reject | 3 | parsed into scrubbed trading diagnostics | ready to observe |
| Business Reject | j | parsed into scrubbed trading diagnostics | ready to observe |
| Sequence Reset | 4 | not yet operator-triggered; trading Logon intentionally does not reset | pending |

## Market-data tests

Existing read-only Centroid market-data integration remains separate from trading. It already supports MarketDataRequest, snapshot/incremental parsing, rejects, and security discovery. The workbook's invalid-symbol, duplicate request ID, subscribe/unsubscribe and depth cases should be run against the existing market-data session during the formal GFC test window.

## Trading tests

| Workbook item | FIX type / values | Implementation | Status before live conformance |
|---|---|---|---|
| Market order | D, 40=1 | `build_new_order_single` + protected conformance endpoint | ready |
| Limit IOC | D, 40=2, 59=3, 44 required | builder validates price | ready |
| Limit FOK | D, 40=2, 59=4, 44 required | builder validates price | ready |
| Execution Report accepted/filled | 8 | parsed and surfaced in diagnostics | ready to observe |
| Execution Report rejected | 8, 150/39=8 | parsed including Text(58) | ready to observe |
| Cancel Request | F | message builder present | route/session action pending |
| Cancel Reject | 9 | parser present | ready to observe |
| Order Status Request | H | message builder present | route/session action pending |
| Cancel/Replace | G | not yet implemented | pending if GFC requires it for this account |

## Required configuration on Railway before connection

Set these only in the Railway FIX worker environment; do not put credentials in source control:

- `CENTROID_TD_HOST`
- `CENTROID_TD_PORT`
- `CENTROID_TD_USERNAME`
- `CENTROID_TD_PASSWORD`
- `CENTROID_TD_SENDER_COMP_ID`
- `CENTROID_TD_TARGET_COMP_ID`
- `CENTROID_TD_ACCOUNT`
- `CENTROID_TD_SSL` as instructed by GFC
- `CENTROID_TD_ENABLED=true` only for the scheduled demo/conformance window
- `CENTROID_TD_CONFORMANCE_MODE=true`

The Railway worker and Vercel application must share the existing admin/cron server-to-server secret so Vercel can proxy protected conformance controls without exposing GFC credentials.

## Formal conformance sequence

1. Deploy this branch to a non-production/controlled worker or merge only after code review.
2. Configure demo trading credentials on Railway.
3. Confirm trading status shows TCP connected and Logon accepted.
4. Observe heartbeat/test-request behavior.
5. Run workbook market-data cases without changing the working production symbol configuration permanently.
6. Run the three NewOrderSingle cases explicitly requested by the workbook, using GFC-approved demo symbol, quantity and prices.
7. Capture corresponding Execution Reports and rejects.
8. Run cancel/status cases after their operator actions are added.
9. Record each workbook row as pass/fail with timestamp and scrubbed FIX evidence.
10. Keep automated/model-driven live trading disabled after conformance.
