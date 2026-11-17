# USD/MXN: what should I do now?

The dashboard's first card is an independent 20-minute estimate. It refreshes
every 30 seconds through the authenticated, read-only `GET /decision/now` route.
It shows BUY USD / SELL MXN NOW, SELL USD / BUY MXN NOW, or WAIT, together with:

- Live FIX midpoint and expected rate 20 minutes from generation.
- Signed expected change in MXN per USD and Mexican cents per USD.
- Central 80% of analogous historical outcomes, explicitly uncalibrated.
- Estimated net result on the existing $100,000 reference notional, including
  the current FIX spread at entry and exit and the existing $40 round-trip fees.
- Comparable sample count and earlier walk-forward directional accuracy.

The existing 2–4 hour card remains available as a separately labelled view.

## Live news check

The 20-minute card now polls the configured news provider once per minute per
process. The authenticated `/jobs/news-watch` cron also checks every minute when
the page is closed and stores real headlines with their original publication
time and first-seen database timestamp. Mock fallbacks never enter this check.
Provider delivery latency and subscription entitlements are not established by
the polling interval; the UI exposes this limit and each article's timestamp.

Headlines from the last 20 minutes can confirm or suspend the price forecast.
Fed and Banxico rate actions are interpreted separately; conditional headlines
are not treated as confirmed decisions. Fresh unclear high-impact news,
conflicting news, missing/future timestamps, an unavailable feed, a missing price
reaction, or disagreement with the price forecast produce WAIT and suppress the
numeric target. Duplicate and old articles cannot become fresh catalysts merely
because they were fetched again. Source links use HTTP(S) and plain text only.

The numeric target and walk-forward results remain price based. News is a
confirmation/risk layer, not a calibrated estimate of news-driven magnitude.
The observed change since a pre-headline snapshot does not establish causality.
No news-conditioned predictive accuracy or publication-speed guarantee is claimed.

## Evidence and limits

Only live `centroid_fix` market snapshots enter the historical data. Daily
research data, hourly fallback feeds and synthetic analog libraries cannot
produce this forecast. Five- and fifteen-minute relative price changes select
up to 60 neighboring historical environments within a fixed distance threshold.
At least 30 comparable, non-overlapping observed 20-minute outcomes are required
to calculate the experimental mean and outcome range. Endpoint timing tolerance
is two minutes; gaps above eight minutes are excluded.

Walk-forward checks train exclusively on outcomes completed before the entire
test lookback starts. Test windows do not overlap. An actionable signal requires
at least 30 earlier forecasts in the current direction, a 95% Wilson lower bound
above 50% directional accuracy and positive average simulated net P/L using
today's spread and fees. These are historical simulations, not a live trading
track record or a probability guarantee. There is no borrowed four-hour
confidence score. Slippage and future spread changes are not modeled.

WAIT is also required for a closed/unverified market, invalid or older-than-30s
FIX quote, insufficient intraday history, non-positive estimated net proceeds,
or incomplete economic-calendar coverage/high-impact event risk. Calendar
coverage is refreshed at most every five minutes per process; FIX is refreshed
on every request. A signal expires after 60 seconds; the browser clears the
action and numeric estimates on expiry or refresh failure.

Historical column-only reads are bounded to 20,000 rows over 60 days and reused
for five minutes per process. A separate, bounded 20-minute tail refreshes on
each poll, keeping momentum current without repeatedly downloading the full
historical dataset.

No order is placed. The forecast does not assert that a position exists. The
user-selected target and stop are each 0.05 MXN per USD (five Mexican cents)
from the setup's FIX midpoint, in opposite directions. The card shows the actual
bid/ask entry side and estimates net P/L at both levels with the captured spread
and existing fees. Equal price distances do not imply equal net profit and loss.
The fixed target is separate from the model's expected rate; a smaller remaining
forecast or a target that cannot cover costs forces WAIT. All existing risk and
evidence restrictions still apply, including during the release workflow.

Horizon validation does not establish the probability of hitting this target
before this stop. That probability remains null/uncalibrated, and the UI says so.
Levels are indicative for the displayed setup, not submitted exit orders or a
tracked position. Each refreshed quote creates a new setup; a trade entered from
an earlier setup must retain its original levels and deadline. The exit deadline
is 20 minutes after generation, or 20 minutes after the scheduled report for the
release assessment. Expiry and fetch failure clear the displayed exit levels.

The first production run may show WAIT until sufficient
genuine intraday history and complete live calendar coverage are available.

## Validation

`pytest -q tests/test_short_term_news.py tests/test_short_term_forecast.py tests/test_dashboard_js_load.py tests/test_customer_sso_auth.py`

This checks both quote directions, exact forecast horizon, cost gating,
non-overlapping samples, walk-forward purging, future-data exclusion, mock/hourly
data exclusion, quote freshness, calendar health, JavaScript parsing and clearing
old signals after expiry/feed failure. No forecast accuracy claim is established
by the synthetic test fixtures.
