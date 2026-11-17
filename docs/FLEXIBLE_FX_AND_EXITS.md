# Flexible five-cent opportunities and personal exit watch

The primary card looks for a 0.05 MXN per USD move before a 0.05 adverse move,
over an interval of up to five hours. It does not require the target to be reached
in 20 minutes. The old 20-minute price forecast remains in a collapsed research
view; scheduled-release scenarios remain separate and cannot overwrite the new
opportunity or the user's tracked position.

## Entry evidence

`GET /decision/opportunity` uses only real live Centroid FIX market snapshots and
new observed bid/ask quotes. Five-, fifteen- and sixty-minute price changes and
the dispersion of five-minute returns select comparable historical paths.
Distances use fixed scales, with no normalization fitted to future prices.

Each historical path covers a full five hours and has a full preceding one-hour
lookback. Those entire windows do not overlap. Gaps over two minutes invalidate
a window: the existing five-minute snapshots alone are too sparse for this
first-touch model. A new observation store retains the first usable FIX quote
in each minute, using the source's quote time, not a fabricated current time.
The minute cron accumulates this history even with the dashboard closed.

Both directions are simulated against executable exit sides using today's
constant spread. The first observed target/stop wins, favorable target fills
are capped at the target, stop overshoots are retained, and paths that touch
neither barrier close at the five-hour endpoint for simulation only. Those
timeouts do not count as target hits. $100K notional and $40 total fees match the
existing app's reference economics. Other slippage is unmodeled. Re-anchoring
that would imply non-positive exchange rates is excluded as unavailable.

At least 30 neighbors and 30 earlier walk-forward results for the selected
direction are required. Each test forecast sees only paths that ended before
its entire lookback. Both current neighbors and earlier tests need a positive
approximate 99% lower bound on mean simulated net profit and a 95% Wilson lower
bound above 50% for sampled target-first frequency. These are conservative
historical gates, not claims of calibrated live confidence. Live probability
stays null. Timing is the median among at least ten analogous target hits;
it is conditional on those hits, excludes failed paths, and is not a guaranteed
arrival time. Historical price simulations are not news-conditioned backtests.

Fresh genuine quotes, verified market-open status, live news checks and complete
economic-calendar coverage remain entry restrictions. New entries pause within
15 minutes of a high-impact scheduled report, rather than the old four-hour
blanket restriction. A fresh conflicting or unclear high-impact headline still
blocks entry. A successful poll with old articles is shown as uncertain/quiet
coverage; no fresh catalyst is required for every price setup.

Reads are bounded to 60,000 combined history points over 90 days. Historical
reads cache for five minutes with a fresh 65-minute tail each poll. New models
may show WAIT for an extended period while genuine sufficiently dense history
accumulates; synthetic tests do not establish predictive performance.

## Personal entries and exits

`GET/POST /decision/positions` and `POST /decision/positions/{uuid}/close` require
a named customer session even when general API authentication is disabled or
an administrator key is supplied. The owner is derived server-side from the
signed user ID; callers cannot choose the owner. Reads and closes filter that
owner. Only one open record per owner is permitted by a unique database slot.
Entry request UUIDs make retries idempotent. A repeat close cannot rewrite a
previously recorded fill. Live deployments refuse ephemeral SQLite storage for
this personal journal. New tables are additive through existing `init_db()`.

The user records a trade they already entered, with their actual fill rate.
BUY USD exits use the bid; SELL USD exits use the ask. Target and stop are five
cents from the entered fill, measured on the executable exit side, so a midpoint
touch alone cannot trigger them. This differs from the old research card's
midpoint-to-midpoint levels. Entry, target, stop and five-hour review timestamp
remain immutable through later quotes, new setups and page refreshes.

Exit rules, in priority order:

1. The first **observed** original target or stop triggers TAKE PROFIT or EXIT /
   STOP, retained until the user explicitly records the actual exit. This is
   advice, never a statement that the trade was executed or closed.
2. A gain of at least three cents followed by at least one cent of giveback,
   suggests TAKE PROFIT EARLY when estimated net after fees is positive, or
   EXIT EARLY when estimated net is no longer positive.
3. Opposing five- and fifteen-minute price changes, including a five-minute
   reversal of at least one cent or the spread (whichever is larger), suggest
   EXIT EARLY.
4. Five elapsed hours require REASSESS; no silent extension or automatic close.
5. An approaching high-impact release, conflicting/unclear fresh news,
   observation gaps, or missing price/news context require REASSESS. Mild
   five-minute weakness gives WATCH MOMENTUM. Otherwise HOLD means only that
   no rule triggered; it is not a forecast that the target will be reached.

The three-cent/one-cent profit-protection and reversal rules are transparent
initial heuristics, not optimized or profitability-validated exit policies.
They never widen the original stop. No broker order, protective stop or profit
order is submitted. The user closes on their trading platform and then records
the actual exit rate; journal P/L still uses the reference $100K and $40 fees.

The dashboard checks every 15 seconds and expires exit advice after 40 seconds;
background checks reuse the existing authenticated minute news cron. Quote
freshness is at most 30 seconds. Stale data cannot produce a fresh HOLD or
numeric current P/L; original risk levels remain visible. Observation gaps are
persisted as a warning. Intraminute touches can still be missed. No push, email
or Telegram alert is sent by this feature. Previously observed target/stop
advice is retained with its timestamp if the quote feed later fails.

## Validation

The focused tests cover both trade directions, executable exit sides, order of
barrier touches, timeout handling, spread/stop overshoot, sparse/future/duplicate
history, purged walk-forward training, missing feeds/calendar risk, invariant
position levels, observed exit latching, profit giveback, reversals, five-hour
review, owner isolation, retries, malformed numeric input, production storage
checks, background monitoring and frontend expiration/privacy.
