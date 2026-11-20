# USD/MXN dip and rebound research

This is an independent **paper research** detector, not a change to the macro
forecast, not a calibrated probability, and not an order-execution feature.
No observed historical win rate is claimed by the implementation or its tests.

## Pattern and costs

- The persistent FIX worker polls its in-memory broker bid/ask every second.
  Dashboard polling is for display only; no browser needs to remain open.
- Requires genuine `centroid_fix` USD/MXN quotes at most 10 seconds old.
  Invalid, stale, out-of-order, crossed and nonfinite quotes cannot trigger entries.
  Coverage gaps over 10 seconds reset candle collection. At least 20 consecutive
  completed minute candles are needed after startup/reconnection.
- Watch a bid decline of at least 0.08 from an earlier high in the last 61 candles.
- Require a local higher low, confirmed on both sides, followed by a completed
  one-minute close and current bid above the preceding swing high by 0.001.
- Capture entry at the current ask. Stop is higher-low bid minus 0.003. Target
  is the lesser of the prior high and entry ask plus 0.06. Reject spread >0.012,
  remaining executable reward <0.05 or net reward/risk <1.5.
- Net economics are $100K USD notional and $20 entry + $20 exit: `100000 *
  (1 - entry_ask / exit_bid) - 40`. Spread is already included. Slippage,
  financing and broker conversion differences are not modeled.
- A paper setup retains its original levels and five-hour deadline. Subsequent
  bid observations record target, stop (including observed overshoot), timeout,
  or an unresolved coverage gap. No additional entry for the same trough.
- Rules do not wait for or require a bullish macro forecast. They can therefore
  identify a recovery after bearish news, but can also produce losing rebounds.

## Run and inspect

On the existing always-on FIX worker, after deploying this branch:

```
USE_MOCK_DATA=false
REBOUND_PAPER_ENABLED=true
REBOUND_PAPER_ALERTS=false
```

Keep the existing Centroid settings and real broker symbol. `init_db` creates
the separate `rebound_paper_events` table. The dashboard card reads
`GET /decision/rebound`; Vercel proxies that read to the persistent worker.
Recent research events remain stored across restarts; an unfinished in-memory
paper plan is not resumed or counted as a completed result after restart.

After observing the detector in paper mode, enabling `REBOUND_PAPER_ALERTS=true`
uses the existing `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID`. Messages are clearly
marked PAPER RESEARCH ONLY; no percentage confidence or live execution is shown.
Delivery is best-effort after event persistence, without retries; failures remain
in logs and stored events. DIP notices are throttled to at most one per five
minutes. New troughs may generate fresh research events. Run one monitor worker
process; event keys suppress duplicate persisted events across processes, but
pattern history is intentionally process-local.

If Telegram credentials are held only by the web app, set a new random
`REBOUND_ALERT_RELAY_SECRET` on both services and set the worker's
`REBOUND_ALERT_RELAY_URL` to the web app's `/admin/research/rebound-alert` endpoint.
The relay accepts authenticated events at most 30 seconds old, deduplicates them
durably, and uses only the web app's existing Telegram destination. Bot credentials
stay on the web app. Delivery remains best effort; no retries or orders are sent.

## Validation before any live recommendation

The persistent FIX worker also records one fresh executable USD/MXN quote each
minute in `observed_fix_quotes`, independently of dashboard requests and the
rebound paper switch. It requires Postgres and retries storage failures without
inventing missing prices. `/admin/research/snapshots/history-status` reports
collection health. The five-cent card reports stored observation count and date
range; it still requires 30 independent comparable paths and 30 earlier validation
paths. Recording starts now and does not reconstruct missed historical minutes.

Official schedule coverage uses FRED, the Fed, Banxico, INEGI and TreasuryDirect's
current upcoming-auctions feed. Treasury competitive close times come from the
publisher and use Eastern timezone rules, including holiday changes. Unannounced
times remain date-only events with an explicit full-day risk window. FRED release
pages are paginated and 8:30 a.m. Eastern releases respect daylight saving time.
Missing subfeeds remain PARTIAL with the cause exposed, and no data subscription
is purchased. These official schedules do not supply missing consensus/actual
access for the separate structured release watcher.

`dip_rebound.replay(quotes)` accepts chronological broker bid/ask observations
with `symbol`, `source`, and `updated_at`. It uses exactly the causal live rules;
no future low is used to enter. Quote gaps make affected plans unresolved.
Tests use synthetic paths solely to verify mechanics, never as evidence of profit.

Export representative broker tick history including news spikes, losing trends,
and quiet sessions. Run all qualifying setups with actual spread, fees, latency,
and adverse fills. Fix thresholds using a training period and evaluate a later,
untouched period plus demo forward observations. Report net P/L, drawdown,
target-before-stop, missed observations and delivery latency. Neither the
single screenshot nor these unit tests establishes a trading advantage.
