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
from app.routers import analysis, health, market

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
    version="0.1.0",
    description="Backend-only USD/MXN market intelligence assistant (Phase 1).",
    lifespan=lifespan,
)

app.include_router(health.router)
app.include_router(market.router)
app.include_router(analysis.router)


DASHBOARD_HTML = """<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>AI Trading Assistant — USD/MXN</title>
  <style>
    body { font-family: -apple-system, Segoe UI, Roboto, sans-serif; margin: 0; background:#0b1220; color:#e6edf3; }
    header { padding: 20px 24px; border-bottom:1px solid #1d2740; }
    h1 { font-size: 18px; margin:0; }
    main { padding: 24px; max-width: 880px; margin: 0 auto; display:grid; gap:16px; }
    .card { background:#111a2e; border:1px solid #1d2740; border-radius:12px; padding:18px 20px; }
    .row { display:flex; flex-wrap:wrap; gap:18px; }
    .stat { flex:1; min-width:120px; }
    .stat .k { font-size:12px; color:#8aa0c6; text-transform:uppercase; letter-spacing:.04em; }
    .stat .v { font-size:22px; font-weight:600; margin-top:4px; }
    .tag { display:inline-block; padding:4px 10px; border-radius:999px; font-weight:600; font-size:13px; }
    .BUY_USD { background:#0f3d2e; color:#5be3a0; }
    .SELL_USD { background:#3d1626; color:#ff9bb5; }
    .NO_TRADE { background:#26314d; color:#9fb3d9; }
    ul { margin:8px 0 0; padding-left:18px; }
    button { background:#2563eb; color:#fff; border:0; padding:9px 14px; border-radius:8px; cursor:pointer; font-weight:600; }
    .muted { color:#8aa0c6; font-size:13px; }
  </style>
</head>
<body>
  <header><h1>AI Trading Assistant — USD/MXN <span class="muted">(Phase 1 · mock data)</span></h1></header>
  <main>
    <div class="card">
      <button onclick="refresh()">Refresh analysis</button>
      <span id="ts" class="muted"></span>
    </div>
    <div class="card">
      <div class="row">
        <div class="stat"><div class="k">USD/MXN</div><div class="v" id="px">—</div></div>
        <div class="stat"><div class="k">DXY</div><div class="v" id="dxy">—</div></div>
        <div class="stat"><div class="k">US 10Y</div><div class="v" id="yld">—</div></div>
        <div class="stat"><div class="k">Oil (WTI)</div><div class="v" id="oil">—</div></div>
      </div>
    </div>
    <div class="card">
      <div class="row" style="align-items:center; justify-content:space-between">
        <div><span id="dir" class="tag NO_TRADE">—</span>
          <span id="mom" class="muted" style="margin-left:8px"></span></div>
        <div class="stat" style="text-align:right; flex:0"><div class="k">Confidence</div><div class="v" id="conf">—</div></div>
      </div>
      <p id="summary" style="margin-top:14px"></p>
      <div class="row">
        <div class="stat"><div class="k">Target</div><div class="v" id="tgt">—</div></div>
        <div class="stat"><div class="k">Stretch</div><div class="v" id="str">—</div></div>
        <div class="stat"><div class="k">Stop</div><div class="v" id="stp">—</div></div>
      </div>
      <div style="margin-top:14px">
        <div class="k muted">Key drivers</div>
        <ul id="drivers"></ul>
      </div>
      <p id="risk" class="muted" style="margin-top:12px"></p>
    </div>
  </main>
  <script>
    async function refresh() {
      const r = await fetch('/analysis/usdmxn');
      const d = await r.json();
      const m = d.market || {};
      document.getElementById('px').textContent = m.usdmxn ?? '—';
      document.getElementById('dxy').textContent = m.dxy ?? '—';
      document.getElementById('yld').textContent = (m.treasury_yield ?? '—') + '%';
      document.getElementById('oil').textContent = m.oil ?? '—';
      const dir = document.getElementById('dir');
      dir.textContent = d.direction;
      dir.className = 'tag ' + d.direction;
      document.getElementById('mom').textContent = d.momentum_status || '';
      document.getElementById('conf').textContent = (d.confidence ?? '—') + '/100';
      document.getElementById('summary').textContent = d.summary || '';
      document.getElementById('tgt').textContent = d.target ?? '—';
      document.getElementById('str').textContent = d.stretch_target ?? '—';
      document.getElementById('stp').textContent = d.stop ?? '—';
      const ul = document.getElementById('drivers');
      ul.innerHTML = '';
      (d.key_drivers || []).forEach(x => { const li = document.createElement('li'); li.textContent = x; ul.appendChild(li); });
      document.getElementById('risk').textContent = d.risk_notes || '';
      document.getElementById('ts').textContent = '  Updated ' + new Date().toLocaleTimeString();
    }
    refresh();
  </script>
</body>
</html>"""


@app.get("/", response_class=HTMLResponse, include_in_schema=False)
def dashboard() -> str:
    return DASHBOARD_HTML
