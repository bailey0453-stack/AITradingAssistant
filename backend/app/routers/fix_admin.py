"""Admin FIX endpoints for market data and GFC trading conformance."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field

from app.services.admin_auth import require_admin_auth
from app.services.fix.provider import (
    get_fix_diagnostics,
    get_trading_diagnostics,
    request_fix_security_discovery,
    send_trading_conformance_order,
)

router = APIRouter(prefix="/admin/research/snapshots", tags=["admin-fix"])


class ConformanceOrderRequest(BaseModel):
    symbol: str = Field(min_length=1, max_length=64)
    side: str = Field(pattern="^[12]$")
    quantity: float = Field(gt=0)
    ord_type: str = Field(default="1", pattern="^[123]$")
    time_in_force: str = Field(default="3", pattern="^[134]$")
    price: float | None = Field(default=None, gt=0)
    ttl_ms: int | None = Field(default=None, gt=0, le=600000)


@router.get("/fix-status")
def fix_status() -> dict:
    """Safe FIX MD status for operators (passwords never returned)."""
    return get_fix_diagnostics()


@router.post("/fix-discover-symbols", dependencies=[Depends(require_admin_auth)])
def fix_discover_symbols() -> dict:
    """Ask the live Centroid FIX session for its entitled security list."""
    try:
        return request_fix_security_discovery()
    except ConnectionError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.get("/trading-status", dependencies=[Depends(require_admin_auth)])
def trading_status() -> dict:
    """Return scrubbed diagnostics from the persistent GFC trading session."""
    try:
        return get_trading_diagnostics()
    except RuntimeError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@router.post("/trading-conformance/order", dependencies=[Depends(require_admin_auth)])
def trading_conformance_order(body: ConformanceOrderRequest) -> dict:
    """Send one explicitly requested demo/conformance order only.

    Vercel proxies this call to the Railway FIX worker; GFC trading credentials
    therefore stay on the persistent worker and do not need to be duplicated in
    the serverless application.
    """
    if body.ord_type == "2" and body.price is None:
        raise HTTPException(status_code=422, detail="Limit orders require price")
    try:
        return send_trading_conformance_order(body.model_dump())
    except (ConnectionError, PermissionError, ValueError, RuntimeError) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.get("/conformance-console", response_class=HTMLResponse)
def conformance_console() -> str:
    """Human-in-the-loop demo conformance controls.

    The page never stores or embeds the admin secret. The operator supplies it
    in-memory, and every order still passes through the existing protected
    conformance endpoint and server-side conformance guards.
    """
    return r'''<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>GFC FIX Conformance</title>
<style>
body{margin:0;background:#0b1220;color:#eef3fb;font-family:Inter,system-ui,-apple-system,sans-serif}.wrap{max-width:980px;margin:36px auto;padding:0 20px}.card{background:#111a2d;border:1px solid #26334d;border-radius:14px;padding:20px;margin:14px 0}.muted{color:#9aacbf}.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(220px,1fr));gap:12px}.test{background:#0e1728;border:1px solid #26334d;border-radius:12px;padding:16px}button{width:100%;padding:11px 14px;border:0;border-radius:9px;background:#2d6cdf;color:white;font-weight:700;cursor:pointer}button:disabled{opacity:.45;cursor:not-allowed}input{width:100%;box-sizing:border-box;padding:10px;border-radius:8px;border:1px solid #34425f;background:#09111f;color:#fff}pre{white-space:pre-wrap;word-break:break-word;background:#08101d;border:1px solid #26334d;padding:12px;border-radius:9px;max-height:360px;overflow:auto}.ok{color:#59d98e}.bad{color:#ff7a90}.pill{display:inline-block;padding:4px 8px;border-radius:999px;background:#1c2940;font-size:12px;margin-right:6px}</style></head>
<body><div class="wrap"><h1>GFC / Centroid FIX Conformance</h1><p class="muted">Demo account only. Every order requires an explicit button click. No AI or automated routing is connected to these controls.</p>
<div class="card"><label>Admin key</label><input id="key" type="password" autocomplete="off" placeholder="Enter admin key"><p class="muted">Kept only in this browser tab and sent as a Bearer header.</p><button id="check">Check trading session</button><div id="session" class="muted" style="margin-top:12px">Not checked.</div></div>
<div class="card"><h2>Order tests</h2><p class="muted">Symbol <span class="pill">USDMXN.r</span> Quantity <span class="pill">0.01</span> Side <span class="pill">BUY</span>. Limit tests use a non-marketable price 0.05 below the current FIX bid.</p><div class="grid">
<div class="test"><h3>1. Market Order</h3><p class="muted">NewOrderSingle, Market, IOC.</p><button data-test="market" disabled>Send Market Test</button></div>
<div class="test"><h3>2. Limit IOC</h3><p class="muted">NewOrderSingle, Limit, IOC, non-marketable.</p><button data-test="ioc" disabled>Send Limit IOC Test</button></div>
<div class="test"><h3>3. Limit FOK</h3><p class="muted">NewOrderSingle, Limit, FOK, non-marketable.</p><button data-test="fok" disabled>Send Limit FOK Test</button></div>
</div></div>
<div class="card"><h2>Last result</h2><div id="result" class="muted">No test sent.</div><pre id="details">Waiting.</pre></div>
</div><script>
const $=id=>document.getElementById(id), buttons=[...document.querySelectorAll('[data-test]')];
function hdr(){return {'Content-Type':'application/json','Authorization':'Bearer '+$('key').value.trim()}}
function setButtons(v){buttons.forEach(b=>b.disabled=!v)}
async function getJson(url,opts={}){const r=await fetch(url,opts);let j={};try{j=await r.json()}catch{}if(!r.ok)throw new Error(j.detail||('HTTP '+r.status));return j}
async function check(){setButtons(false);$('session').textContent='Checking...';try{const s=await getJson('./trading-status',{headers:hdr()});const ok=!!s.fix_logged_on && !!s.trading_enabled && !!s.conformance_mode;$('session').innerHTML=ok?'<span class="ok">Logged on and ready for demo conformance.</span>':'<span class="bad">Not ready.</span> '+JSON.stringify({status:s.status,fix_logged_on:s.fix_logged_on,trading_enabled:s.trading_enabled,conformance_mode:s.conformance_mode});setButtons(ok)}catch(e){$('session').innerHTML='<span class="bad">'+e.message+'</span>'}}
async function quote(){const s=await getJson('./fix-status');const bid=Number(s?.quote?.bid);if(!Number.isFinite(bid))throw new Error('No live FIX bid available');return bid}
async function poll(before){for(let i=0;i<10;i++){await new Promise(r=>setTimeout(r,500));const s=await getJson('./trading-status',{headers:hdr()});const er=s.last_execution_report;if(er&&JSON.stringify(er)!==JSON.stringify(before))return {session:s,execution_report:er};if(s.last_business_reject||s.last_session_reject||s.last_cancel_reject)return {session:s,reject:s.last_business_reject||s.last_session_reject||s.last_cancel_reject}}return {timeout:true}}
async function run(kind,btn){if(!confirm('Send this demo/conformance FIX order now?'))return;buttons.forEach(b=>b.disabled=true);$('result').textContent='Sending '+kind+' test...';$('details').textContent='Waiting for Centroid response...';try{const before=(await getJson('./trading-status',{headers:hdr()})).last_execution_report;let body={symbol:'USDMXN.r',side:'1',quantity:0.01,ord_type:'1',time_in_force:'3'};if(kind!=='market'){const bid=await quote();body.ord_type='2';body.time_in_force=kind==='fok'?'4':'3';body.price=Number((bid-0.05).toFixed(5))}const sent=await getJson('./trading-conformance/order',{method:'POST',headers:hdr(),body:JSON.stringify(body)});const outcome=await poll(before);$('result').innerHTML='<span class="ok">Request sent.</span>';$('details').textContent=JSON.stringify({request:body,sent:sent,outcome:outcome},null,2)}catch(e){$('result').innerHTML='<span class="bad">Test failed: '+e.message+'</span>'; $('details').textContent=e.stack||e.message}finally{await check()}}
$('check').onclick=check;buttons.forEach(b=>b.onclick=()=>run(b.dataset.test,b));
</script></body></html>'''
