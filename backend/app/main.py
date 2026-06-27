"""FastAPI application entrypoint.

Run locally:
    uvicorn app.main:app --reload

Endpoints:
    GET /health
    GET /market/usdmxn          (+ /market/usdmxn/history)
    GET /analysis/usdmxn        (+ /analysis/usdmxn/history)
    GET /                        simple HTML dashboard (optional)
"""

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.responses import HTMLResponse

from app.config import get_settings
from app.database import init_db
from app.routers import (
    analysis,
    calendar,
    decision,
    health,
    history,
    market,
    news,
    performance,
    recommendations,
    research,
    timeline,
)

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Never let a DB hiccup take down the whole app — /health must stay up.
    try:
        init_db()
    except Exception:  # noqa: BLE001
        logger.exception(
            "Database initialization failed at startup; continuing so /health "
            "remains available."
        )
    yield


settings = get_settings()

app = FastAPI(
    title=settings.app_name,
    version="0.5.0",
    description=(
        "Backend-only USD/MXN market intelligence assistant "
        "(Phase 5 · evidence-based forecasting)."
    ),
    lifespan=lifespan,
)

app.include_router(health.router)
app.include_router(market.router)
app.include_router(analysis.router)
app.include_router(news.router)
app.include_router(calendar.router)
app.include_router(timeline.router)
app.include_router(history.router)
app.include_router(recommendations.router)
app.include_router(research.router)
app.include_router(performance.router)
app.include_router(decision.router)


DASHBOARD_HTML = """<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>AI Trading Assistant — USD/MXN</title>
  <style>
    body { font-family: -apple-system, Segoe UI, Roboto, sans-serif; margin: 0; background:#0b1220; color:#e6edf3; }
    header { padding: 20px 24px; border-bottom:1px solid #1d2740; display:flex; justify-content:space-between; align-items:center; }
    h1 { font-size: 18px; margin:0; }
    h2 { font-size: 13px; text-transform:uppercase; letter-spacing:.05em; color:#8aa0c6; margin:0 0 12px; }
    main { padding: 24px; max-width: 1040px; margin: 0 auto; display:grid; gap:16px; }
    .grid2 { display:grid; grid-template-columns:1fr 1fr; gap:16px; }
    @media (max-width:760px){ .grid2 { grid-template-columns:1fr; } }
    .card { background:#111a2e; border:1px solid #1d2740; border-radius:12px; padding:18px 20px; }
    .row { display:flex; flex-wrap:wrap; gap:18px; }
    .stat { flex:1; min-width:96px; }
    .stat .k { font-size:11px; color:#8aa0c6; text-transform:uppercase; letter-spacing:.04em; }
    .stat .v { font-size:20px; font-weight:600; margin-top:4px; }
    .tag { display:inline-block; padding:4px 10px; border-radius:999px; font-weight:700; font-size:13px; }
    .BUY_USD { background:#0f3d2e; color:#5be3a0; }
    .SELL_USD { background:#3d1626; color:#ff9bb5; }
    .NO_TRADE { background:#26314d; color:#9fb3d9; }
    .pill { display:inline-block; padding:2px 8px; border-radius:6px; font-size:12px; background:#1b2542; color:#9fb3d9; }
    .pill.high { background:#3d1626; color:#ff9bb5; }
    .pill.elevated { background:#3d3416; color:#ffd98a; }
    .pill.low { background:#0f3d2e; color:#5be3a0; }
    ul { margin:8px 0 0; padding-left:18px; }
    li { margin:3px 0; }
    button { background:#2563eb; color:#fff; border:0; padding:9px 14px; border-radius:8px; cursor:pointer; font-weight:600; }
    .muted { color:#8aa0c6; font-size:13px; }
    .tl { border-left:2px solid #1d2740; padding-left:14px; margin-left:4px; }
    .tl .item { margin-bottom:12px; }
    .tl .label { font-weight:600; }
    .src { font-size:11px; padding:2px 7px; border-radius:6px; background:#1b2542; color:#9fb3d9; }
    .src.live { background:#0f3d2e; color:#5be3a0; }
    .src.imported, .src.backfilled, .src.cached { background:#15324a; color:#7fd0ff; }
    .src.fallback { background:#3d3416; color:#ffd98a; }
    .src.mock, .src.sample { background:#3a2236; color:#f0a6d6; }
    .evb { display:inline-block; padding:0 6px; border-radius:8px; font-size:10px; font-weight:700; letter-spacing:.04em; vertical-align:middle; margin-left:6px; cursor:help; }
    .evb.measured { background:#0f3d2e; color:#5be3a0; border:1px solid #1d6b48; }
    .evb.historical { background:#15324a; color:#7fd0ff; border:1px solid #2a5a7a; }
    .evb.live { background:#0f3d2e; color:#7df0b0; border:1px solid #1d6b48; }
    .evb.cached { background:#3d3416; color:#ffd98a; border:1px solid #6b5e1d; }
    .evb.estimated { background:#26314d; color:#9fb3d9; border:1px solid #34406a; }
    .evb.sample { background:#3a2236; color:#f0a6d6; border:1px solid #6b3a55; }
    .hstat { display:inline-flex; gap:6px; align-items:center; margin:3px 10px 3px 0; font-size:12px; }
    .dot { width:9px; height:9px; border-radius:50%; display:inline-block; background:#5b6b8c; }
    .dot.healthy { background:#5be3a0; } .dot.using_cache { background:#7fd0ff; }
    .dot.using_fallback, .dot.rate_limited { background:#ffd98a; } .dot.offline { background:#ff6b8b; }
    .sources { margin-top:8px; display:flex; gap:8px; flex-wrap:wrap; align-items:center; }
    .sources .lbl { font-size:11px; color:#8aa0c6; }
    table { width:100%; border-collapse:collapse; font-size:13px; }
    th, td { text-align:left; padding:6px 8px; border-bottom:1px solid #1d2740; }
    th { color:#8aa0c6; font-weight:600; text-transform:uppercase; font-size:11px; letter-spacing:.04em; }
    .lean { font-weight:700; }
    .lean-usd { color:#5be3a0; }
    .lean-mxn { color:#ff9bb5; }
    .lean-neutral { color:#9fb3d9; }
    .fac-bull li { color:#9be7c0; }
    .fac-bear li { color:#ffb3c6; }
    a { color:#7aa7ff; text-decoration:none; }
    a:hover { text-decoration:underline; }
    .grade { font-size:34px; font-weight:800; letter-spacing:.02em; }
    .grade-Aplus, .grade-A { color:#5be3a0; }
    .grade-B { color:#9be7c0; }
    .grade-C { color:#ffd98a; }
    .grade-D { color:#ffb3c6; }
    .grade-PASS { color:#9fb3d9; }
    .regime { font-size:20px; font-weight:700; }
  </style>
</head>
<body>
  <header>
    <h1>AI Trading Assistant — USD/MXN <span class="muted">(Phase 5 · evidence-based forecasting)</span></h1>
    <div><span id="src" class="src">—</span> <span id="newssrc" class="src">—</span> <button onclick="refresh()">Refresh</button></div>
    <div class="sources">
      <span class="lbl">Data sources:</span>
      <span>Market <span id="ds_market" class="src">—</span></span>
      <span>News <span id="ds_news" class="src">—</span></span>
      <span>Calendar <span id="ds_cal" class="src">—</span></span>
      <span>Historical <span id="ds_hist" class="src">—</span></span>
    </div>
  </header>
  <main>
    <div class="card">
      <h2>Market</h2>
      <div class="row">
        <div class="stat"><div class="k">USD/MXN <span class="src" id="fs_usdmxn">—</span></div><div class="v" id="px">—<span id="pv_spot_rate"></span></div></div>
        <div class="stat"><div class="k">Inverse</div><div class="v" id="inv">—</div></div>
        <div class="stat"><div class="k">DXY <span class="src" id="fs_dxy">—</span></div><div class="v" id="dxy">—</div></div>
        <div class="stat"><div class="k">US 2Y <span class="src" id="fs_us2y">—</span></div><div class="v" id="us2y">—</div></div>
        <div class="stat"><div class="k">US 10Y <span class="src" id="fs_us10y">—</span></div><div class="v" id="us10y">—</div></div>
        <div class="stat"><div class="k">Oil <span class="src" id="fs_oil">—</span></div><div class="v" id="oil">—</div></div>
        <div class="stat"><div class="k">Gold <span class="src" id="fs_gold">—</span></div><div class="v" id="gold">—</div></div>
        <div class="stat"><div class="k">VIX <span class="src" id="fs_vix">—</span></div><div class="v" id="vix">—</div></div>
      </div>
    </div>

    <div class="grid2">
      <div class="card">
        <h2>Market status <span id="mkt_badge" class="src">—</span></h2>
        <p id="mkt_reason" class="muted" style="margin:4px 0 10px"></p>
        <div class="row">
          <div class="stat"><div class="k">Last live update</div><div class="v" id="mkt_fetched">—</div></div>
          <div class="stat"><div class="k">Data age</div><div class="v" id="mkt_age">—</div></div>
          <div class="stat"><div class="k">Next refresh</div><div class="v" id="mkt_next">—</div></div>
          <div class="stat"><div class="k">Next market open</div><div class="v" id="mkt_open">—</div></div>
          <div class="stat"><div class="k">Provider</div><div class="v" id="mkt_provider">—</div></div>
          <div class="stat"><div class="k">Source</div><div class="v" id="mkt_source">—</div></div>
        </div>
        <p id="mkt_closed_note" class="muted" style="margin-top:10px;font-weight:600"></p>
      </div>
      <div class="card">
        <h2>Provider health</h2>
        <div id="provhealth" style="margin-top:8px"></div>
      </div>
    </div>

    <div class="card" style="border-left:4px solid #7fd0ff">
      <h2>Evidence summary <span class="src sample">provenance</span></h2>
      <p class="muted" style="margin:4px 0 10px">How much of today's analysis is backed by evidence vs inference. Every badge below explains itself on hover.</p>
      <div class="row">
        <div class="stat"><div class="k">Live</div><div class="v" id="es_live">—</div></div>
        <div class="stat"><div class="k">Cached</div><div class="v" id="es_cached">—</div></div>
        <div class="stat"><div class="k">Measured</div><div class="v" id="es_measured">—</div></div>
        <div class="stat"><div class="k">Historical</div><div class="v" id="es_historical">—</div></div>
        <div class="stat"><div class="k">Estimated</div><div class="v" id="es_estimated">—</div></div>
        <div class="stat"><div class="k">Sample</div><div class="v" id="es_sample">—</div></div>
      </div>
      <div class="row" style="margin-top:8px">
        <div class="stat"><div class="k">Evidence-backed</div><div class="v" id="es_backed">—</div></div>
        <div class="stat"><div class="k">Estimated share</div><div class="v" id="es_estshare">—</div></div>
        <div class="stat"><div class="k">Sample share</div><div class="v" id="es_smpshare">—</div></div>
      </div>
      <table style="margin-top:10px"><thead><tr><th>Source</th><th>Level</th><th>Metrics</th><th>Fields</th></tr></thead><tbody id="es_rows"></tbody></table>
    </div>

    <div class="card" style="border-left:4px solid #d69e2e">
      <h2>Decision quality <span class="src sample">trade vs wait</span></h2>
      <p class="muted" style="margin:4px 0 10px">Decision support only — not trading execution. Helps judge whether a setup is worth taking now vs waiting; paper figures are simulated.</p>
      <div class="row">
        <div class="stat"><div class="k">Trade quality score</div><div class="v" id="dq_score">—</div></div>
        <div class="stat"><div class="k">Quality label</div><div class="v" id="dq_label">—</div></div>
        <div class="stat"><div class="k">Should trade now?</div><div class="v" id="dq_should">—</div></div>
        <div class="stat"><div class="k">Reward / risk</div><div class="v" id="dq_rr">—</div></div>
        <div class="stat"><div class="k">Min win rate</div><div class="v" id="dq_minwin">—</div></div>
        <div class="stat"><div class="k">Expected value</div><div class="v" id="dq_ev">—</div></div>
      </div>
      <div class="grid2" style="margin-top:8px">
        <div>
          <div class="k muted">Recommendation</div>
          <p id="dq_reason" style="font-weight:600"></p>
          <div class="k muted">Better entry conditions</div>
          <ul id="dq_better"></ul>
        </div>
        <div>
          <div class="k muted">What to watch next</div>
          <ul id="dq_watch"></ul>
          <div class="k muted">Quality components (weighted)</div>
          <table><thead><tr><th>Component</th><th>Score</th></tr></thead><tbody id="dq_components"></tbody></table>
        </div>
      </div>
      <div class="k muted" style="margin-top:8px">Similar recommendation track record</div>
      <div class="row" id="dq_track_row">
        <div class="stat"><div class="k">Similar count</div><div class="v" id="dq_simn">—</div></div>
        <div class="stat"><div class="k">Win rate</div><div class="v" id="dq_simwin">—</div></div>
        <div class="stat"><div class="k">Avg P/L</div><div class="v" id="dq_simpnl">—</div></div>
        <div class="stat"><div class="k">Target hit</div><div class="v" id="dq_simtgt">—</div></div>
        <div class="stat"><div class="k">Stop hit</div><div class="v" id="dq_simstop">—</div></div>
      </div>
      <p class="muted" id="dq_track_note" style="margin-top:4px"></p>
      <div class="k muted" style="margin-top:8px">Selective trading analysis <span class="tag" style="background:#3a2236;color:#f0a6d6;font-size:11px">SIMULATED</span> — if we only traded ...</div>
      <table>
        <thead><tr><th>Filter</th><th>Trades</th><th>Win%</th><th>Net P/L</th><th>Avg P/L</th><th>Max DD</th><th>Ret/notional</th></tr></thead>
        <tbody id="dq_selective"></tbody>
      </table>
      <p class="muted" id="dq_empty" style="margin-top:6px"></p>
    </div>

    <div class="card">
      <h2>Model performance <span class="muted" id="perf_note">(paper recommendations)</span></h2>
      <div class="row">
        <div class="stat"><div class="k">Recommendations</div><div class="v" id="perf_total">—</div></div>
        <div class="stat"><div class="k">Evaluated</div><div class="v" id="perf_eval">—</div></div>
        <div class="stat"><div class="k">Win rate</div><div class="v" id="perf_win">—</div></div>
        <div class="stat"><div class="k">Target hit</div><div class="v" id="perf_target">—</div></div>
        <div class="stat"><div class="k">Stop hit</div><div class="v" id="perf_stop">—</div></div>
        <div class="stat"><div class="k">Avg return</div><div class="v" id="perf_ret">—</div></div>
      </div>
      <div class="grid2" style="margin-top:10px">
        <div>
          <div class="k muted">By confidence</div>
          <table><thead><tr><th>Bucket</th><th>Win%</th><th>Avg ret</th><th>N</th></tr></thead><tbody id="perf_conf"></tbody></table>
        </div>
        <div>
          <div class="k muted">By grade</div>
          <table><thead><tr><th>Grade</th><th>Win%</th><th>Avg ret</th><th>N</th></tr></thead><tbody id="perf_grade"></tbody></table>
        </div>
      </div>
      <div class="k muted" style="margin-top:10px">By horizon</div>
      <table><thead><tr><th>Horizon</th><th>Win%</th><th>Target%</th><th>Stop%</th><th>Avg ret</th><th>N</th></tr></thead><tbody id="perf_horizon"></tbody></table>
      <p class="muted" id="perf_empty" style="margin-top:8px"></p>
    </div>

    <div class="card" style="border-left:4px solid #6b46c1">
      <h2>AI Research Lab <span class="src sample">self-evaluation</span></h2>
      <div class="row">
        <div class="stat"><div class="k">Recommendations stored</div><div class="v" id="rl_stored">—</div></div>
        <div class="stat"><div class="k">Evaluated recommendations</div><div class="v" id="rl_evaluated">—</div></div>
        <div class="stat"><div class="k">Awaiting evaluation</div><div class="v" id="rl_pending">—</div></div>
        <div class="stat"><div class="k">Recommendation accuracy (1d) <span class="evb measured" title="Computed from stored recommendation outcomes.">MEASURED</span></div><div class="v" id="rl_acc">—</div></div>
        <div class="stat"><div class="k">Signal stability</div><div class="v" id="rl_stab">—</div></div>
        <div class="stat"><div class="k">Recommendation drift</div><div class="v" id="rl_drift">—</div></div>
      </div>
      <div class="k muted" style="margin-top:10px">Pending evaluations by horizon (read-only — evaluated once enough time passes)</div>
      <table><thead><tr><th>Horizon</th><th>1h</th><th>4h</th><th>EOD</th><th>1d</th><th>2d</th><th>5d</th></tr></thead>
        <tbody>
          <tr><td>Evaluated</td><td id="ev_1h">—</td><td id="ev_4h">—</td><td id="ev_eod">—</td><td id="ev_1d">—</td><td id="ev_2d">—</td><td id="ev_5d">—</td></tr>
          <tr><td>Pending</td><td id="pe_1h">—</td><td id="pe_4h">—</td><td id="pe_eod">—</td><td id="pe_1d">—</td><td id="pe_2d">—</td><td id="pe_5d">—</td></tr>
        </tbody>
      </table>
      <div class="k muted" style="margin-top:12px">Self-assessment (observations only — weights never change automatically)</div>
      <ul id="rl_assess"></ul>
      <div class="grid2" style="margin-top:8px">
        <div>
          <div class="k muted">Confidence calibration</div>
          <table><thead><tr><th>Conf</th><th>Predicted</th><th>Actual</th><th>Gap</th><th>N</th></tr></thead><tbody id="rl_calib"></tbody></table>
        </div>
        <div>
          <div class="k muted">Accuracy by grade</div>
          <table><thead><tr><th>Grade</th><th>Acc%</th><th>N</th></tr></thead><tbody id="rl_grade"></tbody></table>
        </div>
      </div>
      <div class="grid2" style="margin-top:8px">
        <div>
          <div class="k muted">Accuracy by regime</div>
          <table><thead><tr><th>Regime</th><th>Acc%</th><th>N</th></tr></thead><tbody id="rl_regime"></tbody></table>
        </div>
        <div>
          <div class="k muted">Accuracy by model version</div>
          <table><thead><tr><th>Version</th><th>Acc%</th><th>Net P/L</th><th>N</th></tr></thead><tbody id="rl_model"></tbody></table>
        </div>
      </div>
      <div class="grid2" style="margin-top:8px">
        <div>
          <div class="k muted">Top drivers</div>
          <table><thead><tr><th>Driver</th><th>Acc%</th><th>N</th></tr></thead><tbody id="rl_topdrv"></tbody></table>
        </div>
        <div>
          <div class="k muted">Weakest drivers</div>
          <table><thead><tr><th>Driver</th><th>Acc%</th><th>N</th></tr></thead><tbody id="rl_weakdrv"></tbody></table>
        </div>
      </div>
      <div class="k muted" style="margin-top:8px">Historical similarity accuracy <span class="evb measured" title="Outcome accuracy grouped by similarity bucket — computed from stored recommendation outcomes.">MEASURED</span></div>
      <table><thead><tr><th>Similarity</th><th>Acc%</th><th>Avg ret</th><th>N</th></tr></thead><tbody id="rl_sim"></tbody></table>
      <div class="k muted" style="margin-top:8px">Provider reliability</div>
      <div id="rl_providers" style="margin-top:6px"></div>
    </div>

    <div class="card" style="border-left:4px solid #2f855a">
      <h2>Paper hedge performance <span class="tag" style="background:#3a2236;color:#f0a6d6;font-size:12px">SIMULATED PAPER PERFORMANCE</span></h2>
      <p class="muted" style="margin:4px 0 10px">$100,000 notional · $40 round-trip cost · BUY_USD / SELL_USD only · no real trades.</p>
      <div class="row">
        <div class="stat"><div class="k">Actionable trades</div><div class="v" id="ph_trades">—</div></div>
        <div class="stat"><div class="k">Win rate</div><div class="v" id="ph_win">—</div></div>
        <div class="stat"><div class="k">Gross P/L</div><div class="v" id="ph_gross">—</div></div>
        <div class="stat"><div class="k">Costs</div><div class="v" id="ph_costs">—</div></div>
        <div class="stat"><div class="k">Net P/L</div><div class="v" id="ph_net">—</div></div>
        <div class="stat"><div class="k">Return on notional</div><div class="v" id="ph_ron">—</div></div>
        <div class="stat"><div class="k">Best trade</div><div class="v lean-usd" id="ph_best">—</div></div>
        <div class="stat"><div class="k">Worst trade</div><div class="v lean-mxn" id="ph_worst">—</div></div>
      </div>
      <div class="k muted" style="margin-top:10px">Monthly net P/L</div>
      <table><thead><tr><th>Month</th><th>Recs</th><th>Actionable</th><th>Win%</th><th>Gross</th><th>Costs</th><th>Net</th><th>Best</th><th>Worst</th></tr></thead><tbody id="ph_monthly"></tbody></table>
    </div>

    <div class="card" style="border-left:4px solid #6b46c1">
      <h2>Recommendation history <span class="src sample">stored signals</span></h2>
      <p class="muted" style="margin:4px 0 10px">Every analysis is stored as a paper recommendation. Horizon cells show evaluation status (Pending until enough time passes). Paper P/L is the 1d simulated net result; NO_TRADE / PASS show N/A.</p>
      <table>
        <thead><tr>
          <th>Time</th><th>Version</th><th>Signal</th><th>Grade</th><th>Conf</th>
          <th>1h</th><th>4h</th><th>EOD</th><th>1d</th><th>2d</th><th>5d</th><th>Paper P/L</th>
        </tr></thead>
        <tbody id="rh_body"></tbody>
      </table>
      <p class="muted" id="rh_empty" style="margin-top:8px"></p>
    </div>

    <div class="card" style="border-left:4px solid #2b6cb0">
      <h2>Strategist brief
        <span class="muted" id="sb_dir">—</span>
        <span class="grade grade-PASS" id="sb_grade" style="font-size:14px; padding:2px 8px; vertical-align:middle">—</span>
        <span class="muted" id="sb_conf">—</span>
      </h2>
      <p id="sb_exec" style="font-size:15px"></p>
      <div class="grid2">
        <div>
          <div class="k muted">Trader action</div>
          <p id="sb_action" style="font-weight:600"></p>
          <div class="k muted">Current trade view</div>
          <p id="sb_view"></p>
        </div>
        <div>
          <div class="k muted">Why this grade</div>
          <p id="sb_whygrade"></p>
          <div class="k muted">Why not higher</div>
          <p id="sb_whyhigher" class="muted"></p>
          <div class="k muted">Why not lower</div>
          <p id="sb_whylower" class="muted"></p>
        </div>
      </div>
      <div class="grid2">
        <div>
          <div class="k muted">Quote guidance (Border Currency ops)</div>
          <ul id="sb_quote"></ul>
        </div>
        <div>
          <div class="k muted">What would change my mind</div>
          <ul id="sb_wwcm"></ul>
        </div>
      </div>
    </div>

    <div class="grid2">
      <div class="card">
        <h2>Opportunity grade</h2>
        <div class="row" style="align-items:center">
          <div class="stat" style="flex:0; min-width:90px">
            <div class="grade grade-PASS" id="grade">—</div>
            <div class="k muted" id="gradescore">—</div>
          </div>
          <div class="stat">
            <div class="k">USD score</div><div class="v lean-usd" id="usdsc2">—</div>
          </div>
          <div class="stat">
            <div class="k">MXN score</div><div class="v lean-mxn" id="mxnsc2">—</div>
          </div>
          <div class="stat">
            <div class="k">Net bias</div><div class="v" id="netsc2">—</div>
          </div>
        </div>
        <div class="k muted" style="margin-top:10px">Why this grade</div>
        <ul id="gradereasons"></ul>
      </div>

      <div class="card">
        <h2>Market regime</h2>
        <div class="row" style="align-items:flex-start">
          <div class="stat">
            <div class="k">Primary</div><div class="regime" id="regprimary">—</div>
          </div>
          <div class="stat">
            <div class="k">Secondary</div><div class="v" id="regsecondary" style="font-size:15px">—</div>
          </div>
          <div class="stat">
            <div class="k">Confidence</div><div class="v" id="regconf">—</div>
          </div>
        </div>
        <div class="k muted" style="margin-top:10px">Read</div>
        <ul id="regrationale"></ul>
      </div>
    </div>

    <div class="grid2">
      <div class="card">
        <h2>Signal</h2>
        <div class="row" style="align-items:center; justify-content:space-between">
          <div><span id="dir" class="tag NO_TRADE">—</span>
            <span id="bias" class="muted" style="margin-left:8px"></span></div>
          <div class="stat" style="text-align:right; flex:0"><div class="k">Trade Score</div><div class="v" id="score">—</div></div>
        </div>
        <div class="row" style="margin-top:14px">
          <div class="stat"><div class="k">Confidence</div><div class="v" id="conf">—</div></div>
          <div class="stat"><div class="k">Momentum</div><div class="v" id="mom" style="font-size:14px">—</div></div>
          <div class="stat"><div class="k">Risk</div><div class="v"><span id="risk" class="pill">—</span></div></div>
        </div>
        <p class="muted" id="hist" style="margin-top:12px"></p>
      </div>

      <div class="card">
        <h2>Primary Trade Plan <span id="pv_plan"></span></h2>
        <div class="row">
          <div class="stat"><div class="k">Entry</div><div class="v" id="entry">—</div></div>
          <div class="stat"><div class="k">Target</div><div class="v" id="tgt">—</div></div>
          <div class="stat"><div class="k">Stretch</div><div class="v" id="str">—</div></div>
          <div class="stat"><div class="k">Stop</div><div class="v" id="stp">—</div></div>
        </div>
        <p class="muted" id="move" style="margin-top:12px"></p>
      </div>
    </div>

    <div class="card">
      <h2>Time Horizon Outlook</h2>
      <table>
        <thead><tr><th>Horizon</th><th>Bias</th><th>Confidence</th><th>Target</th><th>Stretch</th><th>Stop</th><th>Expected Move</th><th>Rationale</th></tr></thead>
        <tbody id="horizons"></tbody>
      </table>
      <p class="muted" style="margin-top:8px">Independent lean per timeframe. The Primary Trade Plan above is the single actionable recommendation; when it is NO_TRADE/PASS these horizons may still show a directional lean.</p>
    </div>

    <div class="card">
      <h2>Summary</h2>
      <p id="summary"></p>
      <div class="k muted" style="margin-top:8px">Key drivers</div>
      <ul id="drivers"></ul>
      <p class="muted" id="risknotes" style="margin-top:12px"></p>
    </div>

    <div class="card">
      <h2>Market drivers</h2>
      <table>
        <thead><tr><th>Indicator</th><th>Value</th><th>Lean</th><th>Why it matters</th></tr></thead>
        <tbody id="mdrivers"></tbody>
      </table>
    </div>

    <div class="grid2">
      <div class="card">
        <h2>Bullish factors (USD)</h2>
        <ul id="bull" class="fac-bull"></ul>
      </div>
      <div class="card">
        <h2>Bearish factors (MXN)</h2>
        <ul id="bear" class="fac-bear"></ul>
      </div>
    </div>

    <div class="card">
      <h2>Key risks (upcoming)</h2>
      <ul id="risks"></ul>
    </div>

    <div class="card">
      <h2>What would change my mind</h2>
      <ul id="wwcm"></ul>
    </div>

    <div class="card">
      <h2>Historical evidence <span id="histsrc" class="src">sample</span><span id="pv_histdb"></span></h2>
      <p id="hevidence" style="margin:6px 0 14px;line-height:1.5"></p>
      <div class="row">
        <div class="stat"><div class="k">Historical similarity</div><div class="v" id="hsim">—<span id="pv_historical_similarity"></span></div></div>
        <div class="stat"><div class="k">Comparable events</div><div class="v" id="hcount">—</div></div>
        <div class="stat"><div class="k">Win rate</div><div class="v" id="hwin">—<span id="pv_historical_win_rate"></span></div></div>
        <div class="stat"><div class="k">Avg move</div><div class="v" id="havg">—</div></div>
        <div class="stat"><div class="k">Median move</div><div class="v" id="hmed">—</div></div>
      </div>
      <div class="row" style="margin-top:12px">
        <div class="stat"><div class="k">Best move</div><div class="v lean-usd" id="hbestmove">—</div></div>
        <div class="stat"><div class="k">Worst move</div><div class="v lean-mxn" id="hworst">—</div></div>
        <div class="stat"><div class="k">Max drawdown</div><div class="v lean-mxn" id="hdd">—</div></div>
        <div class="stat"><div class="k">Reversal prob</div><div class="v" id="hrev">—</div></div>
        <div class="stat"><div class="k">Setup percentile</div><div class="v" id="hpctile">—</div></div>
      </div>
      <div class="row" style="margin-top:12px">
        <div class="stat"><div class="k">Expected holding</div><div class="v" id="hdur" style="font-size:15px">—</div></div>
        <div class="stat"><div class="k">Typical MFE</div><div class="v lean-usd" id="hmfe">—</div></div>
        <div class="stat"><div class="k">Typical MAE</div><div class="v lean-mxn" id="hmae">—</div></div>
        <div class="stat"><div class="k">Expected range</div><div class="v" id="hrange" style="font-size:15px">—</div></div>
      </div>
      <div class="k muted" style="margin-top:12px">Top historical analog</div>
      <p id="hbest" class="muted"></p>
      <div class="k muted" style="margin-top:6px">Probability distribution (evidence-based, 95% CI)</div>
      <table style="margin-top:6px">
        <thead><tr><th>Outcome</th><th>Level</th><th>Probability</th><th>95% CI</th><th>Sample</th></tr></thead>
        <tbody id="hprob"></tbody>
      </table>
      <div class="k muted" style="margin-top:12px">Confidence breakdown</div>
      <ul id="hconf"></ul>
    </div>

    <div class="card">
      <h2>How these numbers are calculated</h2>
      <ul id="explains"></ul>
    </div>

    <div class="card">
      <h2>Signal weighting (debug)</h2>
      <div class="row">
        <div class="stat"><div class="k">USD score</div><div class="v lean-usd" id="usdsc">—</div></div>
        <div class="stat"><div class="k">MXN score</div><div class="v lean-mxn" id="mxnsc">—</div></div>
        <div class="stat"><div class="k">Net</div><div class="v" id="netsc">—</div></div>
        <div class="stat"><div class="k">Threshold</div><div class="v" id="thrsc">—</div></div>
        <div class="stat"><div class="k">Weights</div><div class="v" id="wver" style="font-size:14px">—</div></div>
      </div>
      <table style="margin-top:14px">
        <thead><tr><th>Signal</th><th>Dir</th><th>Weight</th><th>Strength</th><th>Contribution</th><th>Detail</th></tr></thead>
        <tbody id="wcontrib"></tbody>
      </table>
      <div class="k muted" style="margin-top:10px">Conflicting signals</div>
      <ul id="conflicts"></ul>
    </div>

    <div class="grid2">
      <div class="card">
        <h2>Event timeline</h2>
        <div class="tl" id="timeline"></div>
      </div>
      <div class="card">
        <h2>Latest news <span id="news_src" class="src">—</span></h2>
        <ul id="news"></ul>
      </div>
    </div>

    <div class="grid2">
      <div class="card">
        <h2>Upcoming events <span id="cal_src" class="src">—</span></h2>
        <ul id="events"></ul>
      </div>
      <div class="card">
        <h2>Recent releases (24h)</h2>
        <ul id="releases"></ul>
      </div>
    </div>
    <p class="muted" id="ts"></p>
  </main>
  <script>
    const $ = id => document.getElementById(id);
    function fill(id, v, suffix){ $(id).textContent = (v ?? v === 0) ? (v + (suffix||'')) : '—'; }
    function setSrc(id, val){ const el=$(id); if(!el) return; const v=(val||'unknown'); el.textContent=v; el.className='src '+v; }
    function fmtTime(iso){ if(!iso) return '—'; try{ const d=new Date(iso); return isNaN(d)?iso:d.toLocaleString(); }catch(e){ return iso; } }
    async function refresh() {
      const d = await (await fetch('/analysis/usdmxn')).json();
      const m = d.market || {};
      fill('px', m.usdmxn); fill('inv', m.inverse_usdmxn); fill('dxy', m.dxy);
      fill('us2y', m.us2y, '%'); fill('us10y', m.us10y, '%'); fill('oil', m.oil);
      fill('gold', m.gold); fill('vix', m.vix);
      $('src').textContent = 'source: ' + (m.source || '—') + ' · ' + (m.provider || '');

      // Per-market-field provenance badges (live/fallback/mock).
      const fsrc = m.sources || {};
      ['usdmxn','dxy','us2y','us10y','oil','gold','vix'].forEach(function(f){
        setSrc('fs_'+f, fsrc[f]);
      });

      // Market status panel (open/closed, age, next refresh/open, provider).
      const ms = d.market_state || {};
      const mb = $('mkt_badge'); mb.textContent = ms.market_status || '—';
      mb.className = 'src ' + (ms.is_open ? 'live' : 'fallback');
      $('mkt_reason').textContent = ms.market_reason || '';
      $('mkt_fetched').textContent = fmtTime(ms.fetched_at);
      $('mkt_age').textContent = (ms.age_minutes==null?'—':(ms.age_minutes+' min'+(ms.is_stale?' · stale':'')));
      $('mkt_next').textContent = fmtTime(ms.next_refresh);
      $('mkt_open').textContent = ms.is_open ? 'open now' : fmtTime(ms.next_market_open);
      $('mkt_provider').textContent = m.provider || '—';
      $('mkt_source').textContent = (ms.cached ? 'cached · ' : '') + (m.source || '—');
      $('mkt_closed_note').textContent = ms.is_open ? '' : 'Using latest available market data.';

      // Provider health panel.
      const ph = d.provider_health || {}; const box = $('provhealth'); box.innerHTML='';
      Object.keys(ph).forEach(function(k){
        const r = ph[k] || {}; const s = document.createElement('span'); s.className='hstat';
        const label = (r.status||'').replace(/_/g,' ');
        s.innerHTML = '<span class="dot '+(r.status||'')+'"></span><b>'+k+'</b>: '+label+(r.detail?(' <span class="muted">('+r.detail+')</span>'):'');
        box.appendChild(s);
      });
      if (!Object.keys(ph).length) box.innerHTML = '<span class="muted">No provider activity yet.</span>';

      // Clearly label every data source (live/mock/fallback/imported/sample).
      const ds = d.data_sources || {};
      setSrc('ds_market', ds.market || m.source);
      setSrc('ds_news', ds.news);
      setSrc('ds_cal', ds.calendar);
      setSrc('ds_hist', ds.historical);
      setSrc('news_src', ds.news);
      setSrc('cal_src', ds.calendar);

      const dir = $('dir'); dir.textContent = d.direction; dir.className = 'tag ' + d.direction;
      $('bias').textContent = d.market_bias || '';
      fill('score', d.trade_score, '/100');
      fill('conf', d.confidence, '/100');
      $('mom').textContent = d.momentum_status || '—';
      const risk = $('risk'); risk.textContent = d.risk_level || '—'; risk.className = 'pill ' + (d.risk_level || '');
      const hs = d.historical_similarity || {};
      $('hist').textContent = 'Historical similarity: ' + (hs.note || 'n/a') + ' (samples: ' + (hs.sample_size ?? 0) + ')';

      fill('entry', d.entry); fill('tgt', d.target); fill('str', d.stretch_target); fill('stp', d.stop);
      $('move').textContent = 'Expected move: ' + (d.expected_move || '—');

      const hz = $('horizons'); hz.innerHTML = '';
      (d.time_horizons || []).forEach(h => {
        const tr = document.createElement('tr');
        const bias = h.bias || 'NO_TRADE';
        tr.innerHTML = '<td><b>'+(h.horizon||'')+'</b></td>'+
          '<td><span class="tag '+bias+'" style="font-size:11px; padding:2px 8px">'+bias+'</span></td>'+
          '<td>'+(h.confidence != null ? h.confidence+'/100' : '—')+'</td>'+
          '<td>'+(h.target ?? '—')+'</td>'+
          '<td>'+(h.stretch_target ?? '—')+'</td>'+
          '<td>'+(h.stop ?? '—')+'</td>'+
          '<td class="muted">'+(h.expected_move||'—')+'</td>'+
          '<td class="muted">'+(h.rationale||'')+'</td>';
        hz.appendChild(tr);
      });
      if (!(d.time_horizons||[]).length) hz.innerHTML = '<tr><td colspan="8" class="muted">No horizon data.</td></tr>';

      $('summary').textContent = d.summary || '';
      const ul = $('drivers'); ul.innerHTML = '';
      (d.key_drivers || []).forEach(x => { const li=document.createElement('li'); li.textContent=x; ul.appendChild(li); });
      $('risknotes').textContent = d.risk_notes || '';

      const leanClass = l => (l||'').startsWith('USD') ? 'lean-usd' : ((l||'').startsWith('MXN') ? 'lean-mxn' : 'lean-neutral');

      // Opportunity grade + regime (Phase 3.5 reasoning layer)
      const gd = d.opportunity_grade_detail || {};
      const grade = d.opportunity_grade || 'PASS';
      const gEl = $('grade');
      gEl.textContent = grade;
      gEl.className = 'grade grade-' + (grade === 'A+' ? 'Aplus' : grade);
      fill('gradescore', gd.score, '/100');
      const sb0 = d.signal_breakdown || {};
      fill('usdsc2', sb0.usd_score); fill('mxnsc2', sb0.mxn_score); fill('netsc2', sb0.net_score);
      const listIntoEl = (id, arr, empty) => {
        const el=$(id); el.innerHTML='';
        (arr||[]).forEach(x => { const li=document.createElement('li'); li.textContent=x; el.appendChild(li); });
        if (!(arr||[]).length) el.innerHTML = '<li class="muted">'+empty+'</li>';
      };
      listIntoEl('gradereasons', gd.reasons, 'No grading detail.');

      // Strategist brief (Phase 4.5)
      $('sb_dir').textContent = d.direction || '—';
      const sbg = $('sb_grade');
      sbg.textContent = grade;
      sbg.className = 'grade grade-' + (grade === 'A+' ? 'Aplus' : grade);
      $('sb_conf').textContent = (d.confidence != null) ? ('confidence ' + d.confidence + '/100') : '';
      $('sb_exec').textContent = d.executive_summary || '—';
      $('sb_action').textContent = d.trader_action || '—';
      $('sb_view').textContent = d.current_trade_view || '—';
      $('sb_whygrade').textContent = d.why_this_grade || '—';
      $('sb_whyhigher').textContent = d.why_not_higher || '—';
      $('sb_whylower').textContent = d.why_not_lower || '—';
      listIntoEl('sb_quote', d.quote_guidance, 'No specific guidance.');
      listIntoEl('sb_wwcm', d.what_would_change_my_mind, 'No invalidation conditions identified.');

      // Decision quality (Phase 5.3)
      renderDecision(d.decision_quality);
      loadSelective();

      // Evidence & provenance (Phase 5.4)
      renderProvenance(d);

      const reg = d.market_regime || {};
      $('regprimary').textContent = reg.primary || '—';
      $('regsecondary').textContent = reg.secondary || '—';
      fill('regconf', reg.confidence, '%');
      listIntoEl('regrationale', reg.rationale, 'No dominant regime read.');

      listIntoEl('wwcm', d.what_would_change_my_mind, 'No invalidation conditions identified.');

      // Historical evidence (Phase 5)
      const h = d.historical || {};
      const hstats = h.statistics || {};
      setSrc('histsrc', (d.data_sources||{}).historical || h.historical_source || 'sample');
      const pct = v => (v === null || v === undefined) ? '—' : ((v>0?'+':'') + v + '%');
      $('hevidence').textContent = d.evidence_summary || h.evidence_summary || 'Insufficient comparable history for an evidence-based read.';
      $('hsim').textContent = (h.best_similarity != null) ? Math.round(h.best_similarity*100)+'%' : '—';
      fill('hcount', h.sample_size);
      $('hwin').textContent = (hstats.win_rate != null) ? hstats.win_rate+'%' : '—';
      $('havg').textContent = pct(hstats.average_move);
      $('hmed').textContent = pct(hstats.median_move);
      $('hbestmove').textContent = pct(hstats.best_move);
      $('hworst').textContent = pct(hstats.worst_move);
      $('hdd').textContent = (hstats.max_drawdown != null) ? ('-'+hstats.max_drawdown+'%') : '—';
      $('hrev').textContent = (hstats.reversal_probability != null) ? hstats.reversal_probability+'%' : '—';
      $('hpctile').textContent = (h.setup_percentile != null) ? h.setup_percentile+'th' : '—';
      $('hdur').textContent = hstats.expected_duration || '—';
      $('hmfe').textContent = pct(hstats.typical_MFE);
      $('hmae').textContent = (hstats.typical_MAE != null) ? ('-'+hstats.typical_MAE+'%') : '—';
      const er = hstats.expected_range;
      $('hrange').textContent = er ? (pct(er.low_pct)+' … '+pct(er.high_pct) + (er.low_price?(' ('+er.low_price+'–'+er.high_price+')'):'')) : '—';
      const bm = (h.top_matches || [])[0];
      $('hbest').textContent = bm ? ((bm.event_name||bm.event_type)+' · '+(bm.release_time||'').slice(0,10)+
        ' · similarity '+Math.round((bm.similarity_score||0)*100)+'% (dist '+(bm.distance_score ?? '—')+') · 1d '+pct((bm.windows||{})['1d'])+
        ' · '+(bm.reversal_behavior||'')) : 'No comparable events yet.';

      const hp = $('hprob'); hp.innerHTML='';
      const probObj = (d.probabilities || {});
      const probs = probObj.levels || {};
      const targets = probObj.targets || {};
      const probEv = probObj.evidence || {};
      const ci = e => (e && e.confidence_interval) ? (e.confidence_interval[0]+'–'+e.confidence_interval[1]+'%') : '—';
      const ss = e => (e && e.sample_size != null) ? e.sample_size : '—';
      const probRows = [
        ['Reaches target 1', targets.target_1, probs.probability_reaches_target_1, probEv.reaches_target],
        ['Reaches target 2', targets.target_2, probs.probability_reaches_target_2, null],
        ['Reaches stretch', targets.stretch, probs.probability_reaches_stretch, probEv.reaches_stretch],
        ['Hits stop', targets.stop, probs.probability_hits_stop, probEv.reaches_stop],
        ['Positive by EOD', null, probs.probability_finishes_positive_today, probEv.finishes_positive_today],
        ['Positive next day', null, probs.probability_finishes_positive_tomorrow, probEv.finishes_positive_tomorrow],
        ['Positive within 5d', null, probs.probability_finishes_positive_within_5d, probEv.finishes_positive_within_5d],
      ];
      probRows.forEach(([label, lvl, p, e]) => {
        if (p == null && lvl == null) return;
        const tr=document.createElement('tr');
        tr.innerHTML = '<td>'+label+'</td><td>'+(lvl ?? '—')+'</td><td>'+(p != null ? p+'%' : '—')+'</td>'+
          '<td class="muted">'+ci(e)+'</td><td class="muted">'+ss(e)+'</td>';
        hp.appendChild(tr);
      });
      if (!hp.children.length) hp.innerHTML = '<tr><td colspan="5" class="muted">No probability data.</td></tr>';

      const cb = (d.confidence_breakdown || {});
      const expl = cb.explanation || [];
      const hc = $('hconf'); hc.innerHTML='';
      expl.forEach(line => { const li=document.createElement('li'); li.textContent = line; hc.appendChild(li); });
      if (cb.value != null) { const li=document.createElement('li'); li.innerHTML='<b>Blended confidence: '+cb.value+'/100</b>'+(cb.formula?(' — <span class="muted">'+cb.formula+'</span>'):''); hc.appendChild(li); }
      if (!hc.children.length) hc.innerHTML = '<li class="muted">Confidence uses the weighted signal only.</li>';

      // Explain every number (Phase 5)
      const ex = d.explanations || {};
      const exMap = {trade_score:'Trade score', confidence:'Confidence', opportunity_grade:'Opportunity grade', historical_similarity:'Historical similarity', probability:'Probability'};
      const exEl = $('explains'); exEl.innerHTML='';
      Object.keys(exMap).forEach(k => {
        if (!ex[k]) return;
        const li=document.createElement('li');
        li.style.marginBottom='8px';
        li.innerHTML = '<b>'+exMap[k]+':</b> '+ex[k];
        exEl.appendChild(li);
      });
      if (!exEl.children.length) exEl.innerHTML = '<li class="muted">Run an analysis to see score derivations.</li>';
      const md = $('mdrivers'); md.innerHTML = '';
      (d.market_drivers || []).forEach(x => {
        const tr=document.createElement('tr');
        tr.innerHTML = '<td>'+(x.name||'')+'</td><td>'+(x.value ?? '—')+'</td>'+
          '<td class="lean '+leanClass(x.lean)+'">'+(x.lean||'neutral')+'</td>'+
          '<td class="muted">'+(x.note||'')+'</td>';
        md.appendChild(tr);
      });
      if (!(d.market_drivers||[]).length) md.innerHTML = '<tr><td colspan="4" class="muted">No driver data.</td></tr>';

      const listInto = (id, arr, empty) => {
        const el=$(id); el.innerHTML='';
        (arr||[]).forEach(x => { const li=document.createElement('li'); li.textContent=x; el.appendChild(li); });
        if (!(arr||[]).length) el.innerHTML = '<li class="muted">'+empty+'</li>';
      };
      listInto('bull', d.bullish_factors, 'No USD-supportive factors right now.');
      listInto('bear', d.bearish_factors, 'No MXN-supportive factors right now.');

      const rk = $('risks'); rk.innerHTML='';
      (d.upcoming_risks || []).forEach(r => {
        const li=document.createElement('li');
        const when = r.hours_away != null ? (' · ~'+r.hours_away+'h') : '';
        li.innerHTML = (r.event||'') + ' <span class="pill '+(r.importance||'')+'">'+(r.importance||'')+'</span>'+
          ' <span class="muted">('+(r.country||'')+when+') — '+(r.note||'')+'</span>';
        rk.appendChild(li);
      });
      if (!(d.upcoming_risks||[]).length) rk.innerHTML = '<li class="muted">No high-impact events flagged.</li>';

      const sb = d.signal_breakdown || {};
      fill('usdsc', sb.usd_score); fill('mxnsc', sb.mxn_score); fill('netsc', sb.net_score); fill('thrsc', sb.trade_threshold);
      $('wver').textContent = sb.weights_version || '—';
      const wc = $('wcontrib'); wc.innerHTML='';
      (d.weighted_contributions||[]).forEach(c => {
        const tr=document.createElement('tr');
        tr.innerHTML = '<td>'+(c.label||'')+'</td>'+
          '<td class="lean '+leanClass(c.direction)+'">'+(c.direction||'')+'</td>'+
          '<td>'+(c.weight ?? '—')+'</td><td>'+(c.strength ?? '—')+'</td>'+
          '<td class="lean '+leanClass(c.direction)+'">'+(c.contribution ?? '—')+'</td>'+
          '<td class="muted">'+(c.detail||'')+'</td>';
        wc.appendChild(tr);
      });
      if (!(d.weighted_contributions||[]).length) wc.innerHTML = '<tr><td colspan="6" class="muted">No active signals.</td></tr>';
      const cf = $('conflicts'); cf.innerHTML='';
      (d.conflicting_signals||[]).forEach(c => {
        const li=document.createElement('li');
        li.innerHTML = (c.label||'') + ' <span class="lean '+leanClass(c.direction)+'">'+(c.direction||'')+'</span>'+
          ' <span class="muted">'+(c.detail||'')+'</span>';
        cf.appendChild(li);
      });
      if (!(d.conflicting_signals||[]).length) cf.innerHTML = '<li class="muted">None.</li>';

      const tl = $('timeline'); tl.innerHTML = '';
      (d.timeline || []).forEach(e => {
        const div=document.createElement('div'); div.className='item';
        div.innerHTML = '<div class="label">'+(e.label||'')+'</div><div class="muted">'+(e.detail||'')+'</div>';
        tl.appendChild(div);
      });
      if (!(d.timeline||[]).length) tl.innerHTML = '<div class="muted">Not enough history yet — refresh again.</div>';

      const ctx = d.context || {};
      const nu = $('news'); nu.innerHTML='';
      (ctx.recent_news||[]).slice(0,6).forEach(n => {
        const li=document.createElement('li');
        const head = n.url ? '<a href="'+n.url+'" target="_blank" rel="noopener">'+(n.headline||'')+'</a>' : (n.headline||'');
        const rel = (n.relevance_score===0||n.relevance_score) ? ' <span class="pill">rel '+n.relevance_score+'</span>' : '';
        li.innerHTML = head + ' <span class="pill '+(n.importance||'')+'">'+(n.importance||'')+'</span>'+ rel +
          ' <span class="muted">'+(n.source||'')+'</span>';
        nu.appendChild(li);
      });
      if (!(ctx.recent_news||[]).length) nu.innerHTML = '<li class="muted">No recent news.</li>';

      const ev = $('events'); ev.innerHTML='';
      (ctx.upcoming_events||[]).slice(0,8).forEach(e => {
        const li=document.createElement('li');
        li.innerHTML = (e.event||'') + ' <span class="muted">('+(e.country||'')+', '+(e.importance||'')+
          ', fc '+(e.forecast ?? 'n/a')+')</span>';
        ev.appendChild(li);
      });
      if (!(ctx.upcoming_events||[]).length) ev.innerHTML = '<li class="muted">No upcoming events.</li>';

      const rel = $('releases'); rel.innerHTML='';
      (ctx.released_last_24h||[]).forEach(e => {
        const li=document.createElement('li');
        li.innerHTML = (e.event||'') + ' <span class="muted">(act '+(e.actual ?? 'n/a')+
          ' vs fc '+(e.forecast ?? 'n/a')+')</span>';
        rel.appendChild(li);
      });
      if (!(ctx.released_last_24h||[]).length) rel.innerHTML = '<li class="muted">No releases in the last 24h.</li>';

      $('newssrc').textContent = 'news: ' + ((d.data_sources||{}).news || '—') + ' · ' + (ctx.recent_news||[]).length + ' items';
      $('ts').textContent = 'Updated ' + new Date().toLocaleTimeString();
      loadPerformance();
    }

    function pctTxt(v){ return (v==null)?'—':(v+'%'); }
    function retTxt(v){ return (v==null)?'—':((v>0?'+':'')+v+'%'); }
    // Reads already-scored outcomes only; no evaluation is triggered on load.
    async function loadPerformance(){
      let p;
      try { p = await (await fetch('/recommendations/performance')).json(); }
      catch(e){ return; }
      $('perf_total').textContent = p.total_recommendations ?? '—';
      $('perf_eval').textContent = p.evaluated_outcomes ?? '—';
      $('perf_win').textContent = pctTxt(p.win_rate);
      $('perf_target').textContent = pctTxt(p.target_hit_rate);
      $('perf_stop').textContent = pctTxt(p.stop_hit_rate);
      $('perf_ret').textContent = retTxt(p.avg_return_pct);
      const confBody=$('perf_conf'); confBody.innerHTML='';
      Object.keys(p.by_confidence||{}).forEach(function(k){
        const a=p.by_confidence[k];
        confBody.innerHTML += '<tr><td>'+k+'</td><td>'+pctTxt(a.win_rate)+'</td><td>'+retTxt(a.avg_return_pct)+'</td><td>'+a.samples+'</td></tr>';
      });
      const gradeBody=$('perf_grade'); gradeBody.innerHTML='';
      Object.keys(p.by_grade||{}).forEach(function(k){
        const a=p.by_grade[k];
        gradeBody.innerHTML += '<tr><td>'+k+'</td><td>'+pctTxt(a.win_rate)+'</td><td>'+retTxt(a.avg_return_pct)+'</td><td>'+a.samples+'</td></tr>';
      });
      const horBody=$('perf_horizon'); horBody.innerHTML='';
      Object.keys(p.by_horizon||{}).forEach(function(k){
        const a=p.by_horizon[k];
        horBody.innerHTML += '<tr><td>'+k+'</td><td>'+pctTxt(a.win_rate)+'</td><td>'+pctTxt(a.target_hit_rate)+'</td><td>'+pctTxt(a.stop_hit_rate)+'</td><td>'+retTxt(a.avg_return_pct)+'</td><td>'+a.samples+'</td></tr>';
      });
      $('perf_empty').textContent = (p.evaluated_outcomes? '' :
        'No outcomes scored yet — recommendations are evaluated once enough time passes (POST /recommendations/evaluate).');
      loadResearch();
    }

    function usd(v){ return (v==null)?'—':('$'+Number(v).toLocaleString(undefined,{maximumFractionDigits:2})); }
    function accRows(obj, cols){
      // cols: array of functions(name, block) -> cell text
      return Object.keys(obj||{}).map(function(k){
        const a=obj[k]||{};
        return '<tr>'+cols.map(function(fn){return '<td>'+fn(k,a)+'</td>';}).join('')+'</tr>';
      }).join('');
    }
    async function loadResearch(){
      let s, ph, mo;
      try {
        s = await (await fetch('/research/summary')).json();
        ph = await (await fetch('/performance/summary')).json();
        mo = await (await fetch('/performance/monthly')).json();
      } catch(e){ return; }

      const ep = s.evaluation_progress || {};
      $('rl_stored').textContent = ep.recommendations_stored ?? '—';
      $('rl_evaluated').textContent = ep.recommendations_evaluated ?? '—';
      $('rl_pending').textContent = ep.recommendations_pending ?? '—';
      const evB = ep.evaluated_by_horizon || {}, peB = ep.pending_by_horizon || {};
      const hmap = {'1h':'1h','4h':'4h','end_of_day':'eod','1d':'1d','2d':'2d','5d':'5d'};
      Object.keys(hmap).forEach(function(h){
        const sfx = hmap[h];
        const ev=$('ev_'+sfx), pe=$('pe_'+sfx);
        if(ev) ev.textContent = (evB[h] ?? 0);
        if(pe) pe.textContent = (peB[h] ?? 0);
      });

      $('rl_acc').textContent = pctTxt(s.overall_accuracy);
      $('rl_stab').textContent = pctTxt((s.signal_stability||{}).stability);
      $('rl_drift').textContent = pctTxt((s.signal_stability||{}).drift_rate);
      const al=$('rl_assess'); al.innerHTML='';
      (s.self_assessment||[]).forEach(function(o){ const li=document.createElement('li'); li.textContent=o; al.appendChild(li); });

      $('rl_calib').innerHTML = accRows(s.confidence_calibration, [
        (k)=>k, (k,a)=>pctTxt(a.predicted_confidence), (k,a)=>pctTxt(a.actual_accuracy),
        (k,a)=>(a.gap==null?'—':a.gap), (k,a)=>a.samples]);
      $('rl_grade').innerHTML = accRows(s.accuracy_by_grade, [(k)=>k,(k,a)=>pctTxt(a.accuracy),(k,a)=>a.samples]);
      $('rl_regime').innerHTML = accRows(s.accuracy_by_regime, [(k)=>k,(k,a)=>pctTxt(a.accuracy),(k,a)=>a.samples]);
      $('rl_model').innerHTML = accRows(s.accuracy_by_model_version, [(k)=>k,(k,a)=>pctTxt(a.accuracy),(k,a)=>usd(a.avg_net_pnl_usd),(k,a)=>a.samples]);
      $('rl_sim').innerHTML = accRows(s.accuracy_by_historical_similarity, [(k)=>k,(k,a)=>pctTxt(a.accuracy),(k,a)=>retTxt(a.avg_return_pct),(k,a)=>a.samples]);

      const drv=function(list){ return (list||[]).map(function(d){return '<tr><td>'+d.driver+'</td><td>'+pctTxt(d.accuracy)+'</td><td>'+d.samples+'</td></tr>';}).join(''); };
      $('rl_topdrv').innerHTML = drv(s.top_drivers);
      $('rl_weakdrv').innerHTML = drv(s.weakest_drivers);

      const pv=s.provider_reliability||{}; const pb=$('rl_providers'); pb.innerHTML='';
      Object.keys(pv).forEach(function(k){ const r=pv[k]||{}; const sp=document.createElement('span'); sp.className='hstat'; sp.innerHTML='<span class="dot '+(r.status||'')+'"></span><b>'+k+'</b>: '+((r.status||'').replace(/_/g,' ')); pb.appendChild(sp); });
      if(!Object.keys(pv).length) pb.innerHTML='<span class="muted">No provider activity yet.</span>';

      $('ph_trades').textContent = ph.actionable_trades ?? '—';
      $('ph_win').textContent = pctTxt(ph.win_rate);
      $('ph_gross').textContent = usd(ph.gross_pnl_usd);
      $('ph_costs').textContent = usd(ph.transaction_costs_usd);
      $('ph_net').textContent = usd(ph.net_pnl_usd);
      $('ph_ron').textContent = retTxt(ph.return_on_notional_pct);
      $('ph_best').textContent = usd(ph.best_trade_usd);
      $('ph_worst').textContent = usd(ph.worst_trade_usd);

      const mb=$('ph_monthly'); mb.innerHTML='';
      const months=(mo||{}).months||{};
      Object.keys(months).sort().reverse().forEach(function(k){
        const m=months[k];
        mb.innerHTML += '<tr><td>'+k+'</td><td>'+m.total_recommendations+'</td><td>'+m.actionable_recommendations+'</td><td>'+pctTxt(m.win_rate)+'</td><td>'+usd(m.gross_pnl_usd)+'</td><td>'+usd(m.transaction_costs_usd)+'</td><td>'+usd(m.net_pnl_usd)+'</td><td>'+usd(m.best_trade_usd)+'</td><td>'+usd(m.worst_trade_usd)+'</td></tr>';
      });
      if(!Object.keys(months).length) mb.innerHTML='<tr><td colspan="9" class="muted">No evaluated months yet.</td></tr>';
      loadHistory();
    }

    function statusPill(st){
      const colors={Pending:'#718096',Win:'#2f855a',Target:'#2f855a',Loss:'#c53030',Stop:'#c53030','N/A':'#4a5568'};
      const c=colors[st]||'#4a5568';
      return '<span style="display:inline-block;padding:1px 7px;border-radius:10px;font-size:11px;font-weight:600;background:'+c+'22;color:'+c+';border:1px solid '+c+'66">'+st+'</span>';
    }
    function signalPill(dir){
      const c = dir==='BUY_USD'?'#3182ce':(dir==='SELL_USD'?'#dd6b20':'#718096');
      return '<span style="color:'+c+';font-weight:600">'+(dir||'—')+'</span>';
    }
    // Reads stored recommendation + outcome data only; no evaluation on load.
    async function loadHistory(){
      let h;
      try { h = await (await fetch('/recommendations/history?limit=50')).json(); }
      catch(e){ return; }
      const order = h.horizons || ['1h','4h','end_of_day','1d','2d','5d'];
      const body=$('rh_body'); body.innerHTML='';
      (h.recommendations||[]).forEach(function(r){
        const t = r.created_at ? new Date(r.created_at).toLocaleString([], {month:'short',day:'numeric',hour:'2-digit',minute:'2-digit'}) : '—';
        const hs = r.horizon_status || {};
        const cells = order.map(function(k){ return '<td>'+statusPill(hs[k]||'Pending')+'</td>'; }).join('');
        const pnl = r.actionable ? (r.paper_pnl_usd==null ? '<span class="muted">Pending</span>' : usd(r.paper_pnl_usd)) : '<span class="muted">N/A</span>';
        body.innerHTML += '<tr>'
          + '<td>'+t+'</td>'
          + '<td class="muted">'+(r.model_version||'—')+'</td>'
          + '<td>'+signalPill(r.direction)+'</td>'
          + '<td>'+(r.opportunity_grade||'—')+'</td>'
          + '<td>'+(r.confidence==null?'—':r.confidence)+'</td>'
          + cells
          + '<td>'+pnl+'</td>'
          + '</tr>';
      });
      $('rh_empty').textContent = (h.recommendations||[]).length ? '' :
        'No recommendations stored yet — load /analysis/usdmxn to create one.';
    }

    // --- Evidence & provenance (Phase 5.4) ---
    function evBadge(meta){
      if(!meta || !meta.source) return '';
      const tip = (meta.explanation||'') + (meta.label? (' — '+meta.label):'') + (meta.note? (' '+meta.note):'');
      return '<span class="evb '+meta.source+'" title="'+tip.replace(/"/g,'&quot;')+'">'+(meta.badge||meta.source.toUpperCase())+'</span>';
    }
    function setBadge(id, meta){ const el=$(id); if(el) el.innerHTML = evBadge(meta); }
    function renderProvenance(d){
      const p = d.provenance||{};
      setBadge('pv_spot_rate', p.spot_rate);
      setBadge('pv_plan', p.target);
      setBadge('pv_historical_similarity', p.historical_similarity);
      setBadge('pv_historical_win_rate', p.historical_win_rate);
      // Historical database label badge (Sample / Historical / Measured).
      const hs = p.historical_similarity;
      const hp = $('pv_histdb');
      if(hp) hp.innerHTML = hs ? ('<span class="evb '+hs.source+'" title="'+((hs.explanation||'')).replace(/"/g,'&quot;')+'">'+(hs.label||hs.badge)+'</span>') : '';

      const ov = d.evidence_overview||{};
      const c = ov.counts||{};
      $('es_live').textContent = c.live ?? '—';
      $('es_cached').textContent = c.cached ?? '—';
      $('es_measured').textContent = c.measured ?? '—';
      $('es_historical').textContent = c.historical ?? '—';
      $('es_estimated').textContent = c.estimated ?? '—';
      $('es_sample').textContent = c.sample ?? '—';
      $('es_backed').textContent = (ov.evidence_backed_share_pct==null?'—':ov.evidence_backed_share_pct+'%');
      $('es_estshare').textContent = (ov.estimated_share_pct==null?'—':ov.estimated_share_pct+'%');
      $('es_smpshare').textContent = (ov.sample_share_pct==null?'—':ov.sample_share_pct+'%');
      const body=$('es_rows'); if(body){ body.innerHTML='';
        (ov.order||[]).forEach(function(s){
          const b=(ov.by_source||{})[s]; if(!b) return;
          body.innerHTML += '<tr><td>'+evBadge({source:s,badge:b.badge,explanation:b.explanation})+'</td><td>L'+b.evidence_level+'</td><td>'+b.count+'</td><td class="muted">'+(b.fields||[]).join(', ')+'</td></tr>';
        });
      }
    }

    const DQ_LABELS = {Excellent:'#2f855a',Good:'#38a169',Marginal:'#d69e2e',Poor:'#dd6b20',Wait:'#718096'};
    const HCAP = {signal_strength:'Signal strength',historical_evidence:'Historical evidence',reward_risk:'Reward / risk',event_risk:'Event risk',volatility_fit:'Volatility fit',model_track_record:'Model track record',paper_hedge_similar:'Paper hedge (similar)'};
    function renderDecision(dq){
      if(!dq){ return; }
      const c = DQ_LABELS[dq.trade_quality_label]||'#8aa0c6';
      $('dq_score').textContent = (dq.trade_quality_score==null?'—':dq.trade_quality_score);
      const lbl=$('dq_label'); lbl.textContent = dq.trade_quality_label||'—'; lbl.style.color=c;
      const should=$('dq_should');
      should.textContent = dq.should_trade_now ? 'YES' : 'WAIT';
      should.style.color = dq.should_trade_now ? '#2f855a' : '#dd6b20';
      const rr = dq.reward_risk||{};
      $('dq_rr').textContent = (rr.reward_risk_ratio==null?'—':(rr.reward_risk_ratio+' : 1'));
      $('dq_minwin').textContent = pctTxt(rr.minimum_required_win_rate);
      const ev = dq.expected_value||{};
      $('dq_ev').textContent = (ev.expected_value_usd==null?'—':usd(ev.expected_value_usd));
      $('dq_reason').textContent = dq.should_trade_now ? (dq.reason_to_trade||'') : (dq.reason_to_wait||'');
      const bl=$('dq_better'); bl.innerHTML='';
      (dq.better_entry_conditions||[]).forEach(function(x){ const li=document.createElement('li'); li.textContent=x; bl.appendChild(li); });
      if(!(dq.better_entry_conditions||[]).length) bl.innerHTML='<li class="muted">—</li>';
      const wl=$('dq_watch'); wl.innerHTML='';
      (dq.what_to_watch_next||[]).forEach(function(x){ const li=document.createElement('li'); li.textContent=x; wl.appendChild(li); });
      const comp=dq.components||{}; const cb=$('dq_components'); cb.innerHTML='';
      Object.keys(HCAP).forEach(function(k){
        cb.innerHTML += '<tr><td>'+HCAP[k]+'</td><td>'+(comp[k]==null?'<span class="muted">n/a</span>':comp[k])+'</td></tr>';
      });
      const tr=dq.similar_track_record||{};
      $('dq_simn').textContent = tr.similar_recommendation_count ?? '—';
      $('dq_simwin').textContent = pctTxt(tr.similar_win_rate);
      $('dq_simpnl').textContent = (tr.similar_avg_pnl==null?'—':usd(tr.similar_avg_pnl));
      $('dq_simtgt').textContent = pctTxt(tr.similar_target_hit_rate);
      $('dq_simstop').textContent = pctTxt(tr.similar_stop_hit_rate);
      $('dq_track_note').textContent = tr.note || '';
    }
    // Reads scored outcomes only; never evaluates on load.
    async function loadSelective(){
      let s;
      try { s = await (await fetch('/decision/selective-performance')).json(); }
      catch(e){ return; }
      const rows = [
        ['All actionable', s.all_trades],
        ['Top 10%', (s.filters||{}).top_10pct],
        ['Top 20%', (s.filters||{}).top_20pct],
        ['Top 30%', (s.filters||{}).top_30pct],
        ['Grade A or better', (s.filters||{}).grade_A_or_better],
        ['Grade B or better', (s.filters||{}).grade_B_or_better],
        ['Confidence > 70', (s.filters||{}).confidence_over_70],
        ['Confidence > 80', (s.filters||{}).confidence_over_80],
      ];
      const body=$('dq_selective'); body.innerHTML='';
      rows.forEach(function(r){
        const a=r[1]||{};
        body.innerHTML += '<tr><td>'+r[0]+'</td><td>'+(a.trades??0)+'</td><td>'+pctTxt(a.win_rate)+'</td><td>'+usd(a.net_pnl_usd)+'</td><td>'+(a.avg_pnl_usd==null?'—':usd(a.avg_pnl_usd))+'</td><td>'+usd(a.max_drawdown_usd)+'</td><td>'+retTxt(a.return_on_notional_pct)+'</td></tr>';
      });
      $('dq_empty').textContent = (s.all_trades && s.all_trades.trades) ? '' :
        'No scored actionable trades yet — selective analysis populates as recommendations are evaluated.';
    }
    refresh();
  </script>
</body>
</html>"""


@app.get("/", response_class=HTMLResponse, include_in_schema=False)
def dashboard() -> str:
    return DASHBOARD_HTML
