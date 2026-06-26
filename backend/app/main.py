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
from app.routers import analysis, calendar, health, history, market, news, timeline

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
    version="0.4.5",
    description=(
        "Backend-only USD/MXN market intelligence assistant "
        "(Phase 4.5 · strategist narrative)."
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
    <h1>AI Trading Assistant — USD/MXN <span class="muted">(Phase 4.5 · strategist narrative)</span></h1>
    <div><span id="src" class="src">—</span> <span id="newssrc" class="src">—</span> <button onclick="refresh()">Refresh</button></div>
  </header>
  <main>
    <div class="card">
      <h2>Market</h2>
      <div class="row">
        <div class="stat"><div class="k">USD/MXN</div><div class="v" id="px">—</div></div>
        <div class="stat"><div class="k">Inverse</div><div class="v" id="inv">—</div></div>
        <div class="stat"><div class="k">DXY</div><div class="v" id="dxy">—</div></div>
        <div class="stat"><div class="k">US 2Y</div><div class="v" id="us2y">—</div></div>
        <div class="stat"><div class="k">US 10Y</div><div class="v" id="us10y">—</div></div>
        <div class="stat"><div class="k">Oil</div><div class="v" id="oil">—</div></div>
        <div class="stat"><div class="k">Gold</div><div class="v" id="gold">—</div></div>
        <div class="stat"><div class="k">VIX</div><div class="v" id="vix">—</div></div>
      </div>
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
        <h2>Trade plan</h2>
        <div class="row">
          <div class="stat"><div class="k">Entry</div><div class="v" id="entry">—</div></div>
          <div class="stat"><div class="k">Target</div><div class="v" id="tgt">—</div></div>
          <div class="stat"><div class="k">Stretch</div><div class="v" id="str">—</div></div>
          <div class="stat"><div class="k">Stop</div><div class="v" id="stp">—</div></div>
        </div>
        <p class="muted" id="move" style="margin-top:12px"></p>
        <p class="muted" id="dur"></p>
      </div>
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
      <h2>Historical intelligence <span class="muted" id="histsrc">(sample data)</span></h2>
      <div class="row">
        <div class="stat"><div class="k">Historical similarity</div><div class="v" id="hsim">—</div></div>
        <div class="stat"><div class="k">Comparable events</div><div class="v" id="hcount">—</div></div>
        <div class="stat"><div class="k">Win rate</div><div class="v" id="hwin">—</div></div>
        <div class="stat"><div class="k">Avg move</div><div class="v" id="havg">—</div></div>
        <div class="stat"><div class="k">Median move</div><div class="v" id="hmed">—</div></div>
      </div>
      <div class="row" style="margin-top:12px">
        <div class="stat"><div class="k">Expected holding</div><div class="v" id="hdur" style="font-size:15px">—</div></div>
        <div class="stat"><div class="k">Typical MFE</div><div class="v lean-usd" id="hmfe">—</div></div>
        <div class="stat"><div class="k">Typical MAE</div><div class="v lean-mxn" id="hmae">—</div></div>
        <div class="stat"><div class="k">Expected range</div><div class="v" id="hrange" style="font-size:15px">—</div></div>
      </div>
      <div class="k muted" style="margin-top:12px">Best historical match</div>
      <p id="hbest" class="muted"></p>
      <div class="k muted" style="margin-top:6px">Probability distribution</div>
      <table style="margin-top:6px">
        <thead><tr><th>Outcome</th><th>Level</th><th>Probability</th></tr></thead>
        <tbody id="hprob"></tbody>
      </table>
      <div class="k muted" style="margin-top:12px">Confidence blend</div>
      <ul id="hconf"></ul>
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
        <h2>Latest news</h2>
        <ul id="news"></ul>
      </div>
    </div>

    <div class="grid2">
      <div class="card">
        <h2>Upcoming events</h2>
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
    async function refresh() {
      const d = await (await fetch('/analysis/usdmxn')).json();
      const m = d.market || {};
      fill('px', m.usdmxn); fill('inv', m.inverse_usdmxn); fill('dxy', m.dxy);
      fill('us2y', m.us2y, '%'); fill('us10y', m.us10y, '%'); fill('oil', m.oil);
      fill('gold', m.gold); fill('vix', m.vix);
      $('src').textContent = 'source: ' + (m.source || '—') + ' · ' + (m.provider || '');

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
      $('dur').textContent = 'Expected duration: ' + (d.expected_duration || '—');

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

      const reg = d.market_regime || {};
      $('regprimary').textContent = reg.primary || '—';
      $('regsecondary').textContent = reg.secondary || '—';
      fill('regconf', reg.confidence, '%');
      listIntoEl('regrationale', reg.rationale, 'No dominant regime read.');

      listIntoEl('wwcm', d.what_would_change_my_mind, 'No invalidation conditions identified.');

      // Historical intelligence (Phase 4)
      const h = d.historical || {};
      const hstats = h.statistics || {};
      const pct = v => (v === null || v === undefined) ? '—' : ((v>0?'+':'') + v + '%');
      $('hsim').textContent = (h.best_similarity != null) ? Math.round(h.best_similarity*100)+'%' : '—';
      fill('hcount', h.sample_size);
      $('hwin').textContent = (hstats.win_rate != null) ? hstats.win_rate+'%' : '—';
      $('havg').textContent = pct(hstats.average_move);
      $('hmed').textContent = pct(hstats.median_move);
      $('hdur').textContent = hstats.expected_duration || '—';
      $('hmfe').textContent = pct(hstats.typical_MFE);
      $('hmae').textContent = (hstats.typical_MAE != null) ? ('-'+hstats.typical_MAE+'%') : '—';
      const er = hstats.expected_range;
      $('hrange').textContent = er ? (pct(er.low_pct)+' … '+pct(er.high_pct) + (er.low_price?(' ('+er.low_price+'–'+er.high_price+')'):'')) : '—';
      const bm = (h.top_matches || [])[0];
      $('hbest').textContent = bm ? ((bm.event_name||bm.event_type)+' · '+(bm.release_time||'').slice(0,10)+
        ' · similarity '+Math.round((bm.similarity_score||0)*100)+'% · 1d '+pct((bm.windows||{})['1d'])+
        ' · '+(bm.reversal_behavior||'')) : 'No comparable events yet.';

      const hp = $('hprob'); hp.innerHTML='';
      const probs = (d.probabilities || {}).levels || {};
      const targets = (d.probabilities || {}).targets || {};
      const probRows = [
        ['Reaches target 1', targets.target_1, probs.probability_reaches_target_1],
        ['Reaches target 2', targets.target_2, probs.probability_reaches_target_2],
        ['Reaches stretch', targets.stretch, probs.probability_reaches_stretch],
        ['Hits stop', targets.stop, probs.probability_hits_stop],
      ];
      probRows.forEach(([label, lvl, p]) => {
        if (p == null && lvl == null) return;
        const tr=document.createElement('tr');
        tr.innerHTML = '<td>'+label+'</td><td>'+(lvl ?? '—')+'</td><td>'+(p != null ? p+'%' : '—')+'</td>';
        hp.appendChild(tr);
      });
      if (!hp.children.length) hp.innerHTML = '<tr><td colspan="3" class="muted">No probability data.</td></tr>';

      const cb = (d.confidence_breakdown || {});
      const cc = cb.components || {};
      const labelMap = {signal:'Weighted signal', historical:'Historical similarity', regime:'Market regime', volatility:'Volatility quality', data_quality:'News/calendar quality'};
      const hc = $('hconf'); hc.innerHTML='';
      Object.keys(labelMap).forEach(k => {
        if (cc[k] == null) return;
        const li=document.createElement('li');
        li.textContent = labelMap[k]+': '+cc[k]+'/100';
        hc.appendChild(li);
      });
      if (cb.value != null) { const li=document.createElement('li'); li.innerHTML='<b>Blended confidence: '+cb.value+'/100</b>'; hc.appendChild(li); }
      if (!hc.children.length) hc.innerHTML = '<li class="muted">Confidence uses the weighted signal only.</li>';
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
        li.innerHTML = head + ' <span class="pill '+(n.importance||'')+'">'+(n.importance||'')+'</span>'+
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

      $('newssrc').textContent = 'news items: ' + (ctx.recent_news||[]).length;
      $('ts').textContent = 'Updated ' + new Date().toLocaleTimeString();
    }
    refresh();
  </script>
</body>
</html>"""


@app.get("/", response_class=HTMLResponse, include_in_schema=False)
def dashboard() -> str:
    return DASHBOARD_HTML
