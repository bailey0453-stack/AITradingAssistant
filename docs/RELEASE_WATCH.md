# Economic release watch

The top of FX Intelligence now has an independent release workflow rather than
presenting a price-only estimate as a prediction of the next economic report.

## Before the release

- Show the report, scheduled local time, countdown, source, previous value and
  consensus when available.
- Show conditional above/below-consensus USD/MXN scenarios. These are not a
  prediction of the unreleased number and do not issue a trade.
- Persist the latest expectation actually observed before release. Capture a
  genuine executable FIX baseline during the final minute.

## After the release

- Obtain structured actual values; never infer released numbers from headlines.
- Compare each actual with its frozen pre-release consensus, preserving initial
  actuals and flagging revisions rather than silently rewriting history.
- Assess simultaneous reports together, including conflicts and missing results.
- Require a fresh FIX bid/ask and a confirming observed price reaction.
- A numeric target requires at least ten genuine completed outcomes matching the
  same report series, units, and entire simultaneous surprise combination. Each
  prior observation must have a pre-release consensus, actual received within
  60 seconds, pre-release quote within a minute, and observed FX outcome at 20
  minutes +/- 30 seconds. Sample/reconstructed data cannot enter this history.
- Targets are for 20 minutes after the report, not an ever-extending 20-minute
  horizon. Net estimates use the established $100K notional, captured current
  spread and $40 fees, with slippage excluded. If the remaining move does not
  cover costs, remain on WAIT. A 95% Wilson lower bound must exceed 50% for
  same-combination historical directional support. These counts are not
  calibrated live forecast confidence; no probability is invented.
- Missing or uncalibrated inputs produce WAIT and an explicit reason. A confirmed
  directional reaction can be shown while a numeric magnitude is unavailable.
- Separate new war/tariff/conflicting headlines can still suspend the assessment.

## Data access and cadence

`GET /decision/release` uses the configured Trading Economics or Finnhub economic
calendar credential. Without a calendar credential, an existing Finnhub news
credential can be tested for calendar entitlement. This does not purchase or
upgrade a plan. A 401/403 becomes ACCESS_REQUIRED; generic news access and FRED
calendar dates do not establish actual/consensus access. Official schedule-only
rows can still show a report/countdown with expectations clearly unavailable.
Provider proprietary/model forecasts do not substitute for economist consensus.

Provider calls are cached for five minutes outside release windows and five
seconds near a report. The dashboard polls every five seconds while the release
window is active, otherwise once per minute. Existing authenticated minute cron
records observations when the dashboard is closed. This background cadence
cannot guarantee sub-minute result capture; provider delivery speed remains
unverified. Failed or stale assessments expire into WAIT after 15 seconds.

The release assessment controls the main action during its active window, so
the independent price-only card cannot present a conflicting actionable signal.
No trade is executed and authentication is unchanged.

## Activation limits

Production must verify the current data account's economic-calendar entitlement.
New structured-feed/journal plumbing alone does not establish forecast accuracy.
Without sufficient genuine release/outcome history, the workflow can compare
results and show observed direction but must withhold numeric news targets.

## Validation

`pytest -q tests/test_release_watch.py tests/test_short_term_news.py tests/test_short_term_forecast.py tests/test_dashboard_js_load.py tests/test_customer_sso_auth.py`

Regression checks cover frozen consensus/initial actual, units and series
identity, zero values, publication timing, revisions, missing simultaneous data,
permission errors, future-data exclusion, real-history requirements, dashboard
expiry and JavaScript syntax. Synthetic fixtures establish software behavior,
not real market predictive performance.
