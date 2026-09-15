"""Human-in-the-loop admin and market-data FIX conformance controls."""

from __future__ import annotations

from fastapi import Depends, HTTPException
from fastapi.responses import HTMLResponse

from app.config import get_settings
from app.routers.fix_admin import router
from app.services.admin_auth import require_admin_auth
from app.services.fix.md_conformance import send_md_case, send_test_request
from app.services.fix.provider import _remote_worker_request, get_fix_diagnostics

_ALLOWED_MD_CASES = {"subscribe_one", "subscribe_multiple", "unsubscribe", "wrong_symbol", "duplicate_id"}


def _run_remote_or_local(path: str, local_call):
    settings = get_settings()
    try:
        remote = _remote_worker_request(settings, "POST", path, admin=True)
        if remote is not None:
            remote["remote_worker"] = True
            return remote
        return local_call(settings)
    except (ConnectionError, RuntimeError, ValueError) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.post("/md-conformance/test-request", dependencies=[Depends(require_admin_auth)])
def md_conformance_test_request() -> dict:
    return _run_remote_or_local(
        "/admin/research/snapshots/md-conformance/test-request",
        send_test_request,
    )


@router.post("/md-conformance/request/{case}", dependencies=[Depends(require_admin_auth)])
def md_conformance_request(case: str) -> dict:
    if case not in _ALLOWED_MD_CASES:
        raise HTTPException(status_code=422, detail="Unknown market-data conformance case")
    return _run_remote_or_local(
        f"/admin/research/snapshots/md-conformance/request/{case}",
        lambda settings: send_md_case(settings, case),
    )


@router.get("/md-conformance/status", dependencies=[Depends(require_admin_auth)])
def md_conformance_status() -> dict:
    return get_fix_diagnostics()


@router.get("/md-conformance-console", response_class=HTMLResponse)
def md_conformance_console() -> str:
    return r'''<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>GFC FIX MD Conformance</title><style>
body{margin:0;background:#0b1220;color:#eef3fb;font-family:Inter,system-ui,-apple-system,sans-serif}.wrap{max-width:1050px;margin:36px auto;padding:0 20px}.card{background:#111a2d;border:1px solid #26334d;border-radius:14px;padding:20px;margin:14px 0}.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(220px,1fr));gap:12px}.test{background:#0e1728;border:1px solid #26334d;border-radius:12px;padding:16px}.muted{color:#9aacbf}.ok{color:#59d98e}.bad{color:#ff7a90}button{width:100%;padding:11px 14px;border:0;border-radius:9px;background:#2d6cdf;color:white;font-weight:700;cursor:pointer}button:disabled{opacity:.45;cursor:not-allowed}input{width:100%;box-sizing:border-box;padding:10px;border-radius:8px;border:1px solid #34425f;background:#09111f;color:#fff}pre{white-space:pre-wrap;word-break:break-word;background:#08101d;border:1px solid #26334d;padding:12px;border-radius:9px;max-height:520px;overflow:auto}</style></head>
<body><div class="wrap"><h1>GFC / Centroid FIX Admin + Market Data Conformance</h1><p class="muted">Demo/read-only market-data session. Every test requires an explicit click. No trading orders are sent from this page.</p>
<div class="card"><label>Admin key</label><input id="key" type="password" autocomplete="off"><p class="muted">Kept only in this browser tab.</p><button id="check">Check MD session</button><div id="session" class="muted" style="margin-top:12px">Not checked.</div></div>
<div class="card"><h2>Admin</h2><div class="grid"><div class="test"><h3>Test Request</h3><p class="muted">Send 35=1 and capture the correlated 35=0 heartbeat.</p><button data-kind="test_request" disabled>Send Test Request</button></div></div></div>
<div class="card"><h2>Market Data</h2><div class="grid">
<div class="test"><h3>Subscribe — depth 1</h3><button data-kind="subscribe_one" disabled>Send</button></div>
<div class="test"><h3>Subscribe — multiple depth</h3><p class="muted">Uses MarketDepth 5.</p><button data-kind="subscribe_multiple" disabled>Send</button></div>
<div class="test"><h3>Unsubscribe</h3><p class="muted">Subscribes then unsubscribes the same MDReqID.</p><button data-kind="unsubscribe" disabled>Send</button></div>
<div class="test"><h3>Wrong symbol reject</h3><button data-kind="wrong_symbol" disabled>Send</button></div>
<div class="test"><h3>Duplicate MDReqID reject</h3><button data-kind="duplicate_id" disabled>Send</button></div>
</div></div>
<div class="card"><h2>Last result</h2><div id="result" class="muted">No test sent.</div><pre id="details">Waiting.</pre></div></div>
<script>
const $=id=>document.getElementById(id),buttons=[...document.querySelectorAll('[data-kind]')];
function hdr(){return {'Authorization':'Bearer '+$('key').value.trim()}}
function enable(v){buttons.forEach(b=>b.disabled=!v)}
async function j(url,opts={}){const r=await fetch(url,{...opts,cache:'no-store'});const t=await r.text();let d={};try{d=t?JSON.parse(t):{}}catch{d={raw:t}}if(!r.ok)throw new Error((d.detail||d.raw||('HTTP '+r.status))+' ['+r.status+']');return d}
async function status(){return j('./md-conformance/status',{headers:hdr()})}
async function check(){enable(false);$('session').textContent='Checking...';try{const s=await status();const x=s.session||s.connection||{};const ok=!!(x.fix_logged_on??s.fix_logged_on);$('session').innerHTML=ok?'<span class="ok">Market-data FIX session logged on.</span>':'<span class="bad">Market-data FIX session not ready.</span>';enable(ok)}catch(e){$('session').innerHTML='<span class="bad">'+e.message+'</span>'}}
function matches(s,sent){const h=s.inbound_history||[];if(sent.test_req_id)return h.filter(x=>x.test_req_id===sent.test_req_id);if(sent.md_req_id)return h.filter(x=>x.md_req_id===sent.md_req_id);return []}
async function poll(sent){let s=null,m=[];for(let i=0;i<24;i++){await new Promise(r=>setTimeout(r,500));s=await status();m=matches(s,sent);if(sent.test_req_id&&m.some(x=>x.msg_type==='0'))break;if(sent.case==='wrong_symbol'&&m.some(x=>['Y','j','3'].includes(x.msg_type)))break;if(sent.case==='duplicate_id'&&m.some(x=>['Y','j','3'].includes(x.msg_type)))break;if(['subscribe_one','subscribe_multiple'].includes(sent.case)&&m.some(x=>['W','X'].includes(x.msg_type)))break;if(sent.case==='unsubscribe'&&m.length)break}return {sent,matched_inbound:m,last_inbound:s?.last_inbound,session:s?.session,quote:s?.quote}}
async function run(kind){if(!confirm('Run this read-only FIX conformance test now?'))return;enable(false);$('result').textContent='Running '+kind+'...';$('details').textContent='Waiting for FIX response...';try{const url=kind==='test_request'?'./md-conformance/test-request':'./md-conformance/request/'+kind;const sent=await j(url,{method:'POST',headers:hdr()});const out=await poll(sent);$('result').innerHTML='<span class="ok">Response capture complete.</span>';$('details').textContent=JSON.stringify(out,null,2)}catch(e){$('result').innerHTML='<span class="bad">Test failed: '+e.message+'</span>';$('details').textContent=e.stack||e.message}finally{await check()}}
$('check').onclick=check;buttons.forEach(b=>b.onclick=()=>run(b.dataset.kind));
</script></body></html>'''
