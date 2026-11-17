"""Browser rendering clears expired advice, preserves fixed position risk levels."""
import subprocess

from app.main import DASHBOARD_HTML


def test_flexible_view_and_position_expiry(tmp_path):
    start=DASHBOARD_HTML.index("// Flexible opportunity and personal exit watch.")
    end=DASHBOARD_HTML.index("// End flexible opportunity and personal exit watch.")
    js=DASHBOARD_HTML[start:end]
    harness="""
const elements={};
const $=id=>elements[id] || (elements[id]={textContent:'',value:'',hidden:false,disabled:false});
const setText=(id,v)=>$(id).textContent=String(v);
const tlRate4=v=>v==null?'—':Number(v).toFixed(4);
const fmtTime=v=>v || '—';
let aitaSession={userId:'alice'};
let fetchJson=async()=>{throw new Error('feed down');};
let checkExpiry;
const setInterval=f=>{checkExpiry=f;};
"""+js+"""
(async()=>{
  const expires=new Date(Date.now()+60000).toISOString();
  renderFlexible({action:'BUY USD / SELL MXN',expires_at:expires,setup:{entry_rate:18.5,target_rate:18.55,stop_rate:18.45},timing:{median_minutes:120}});
  if($('fo_target').textContent!=='18.5500' || $('fo_timing').textContent!=='120 min · historical median') throw new Error('flexible setup missing');
  await loadFlexible();
  if($('fo_action').textContent!=='WAIT' || $('fo_target').textContent!=='—') throw new Error('failed opportunity feed retained a signal');
  renderPersonalWatch({position:{id:'one',direction:'BUY_USD',action:'HOLD',expires_at:expires,entry_rate:18.5,target_rate:18.55,stop_rate:18.45,current_exit_rate:18.52,net_pnl_usd:60}});
  personalExpires=Date.now()-1;checkExpiry();
  if($('pe_action').textContent!=='CHECK LIVE QUOTE' || $('pe_net').textContent!=='—') throw new Error('expired position advice retained');
  if($('pe_target').textContent!=='18.5500' || $('pe_stop').textContent!=='18.4500') throw new Error('original risk levels were lost');
  renderPersonalWatch({position:{id:'one',direction:'BUY_USD',action:'HOLD',expires_at:expires,entry_rate:18.5,target_rate:18.55,stop_rate:18.45,current_exit_rate:18.52,net_pnl_usd:60}});
  await loadPersonalWatch();
  if($('pe_action').textContent!=='CHECK LIVE QUOTE' || $('pe_exit').textContent!=='—') throw new Error('failed exit feed retained a quote');
  if(personalTradeId!=='one' || $('pe_stop').textContent!=='18.4500') throw new Error('feed failure forgot the position');
  $('pe_entry_input').value='18.5';$('pe_exit_input').value='18.55';
  resetPersonalWatch();
  if($('pe_entry_input').value!=='18.5') throw new Error('background refresh erased typed entry');
  resetPersonalWatch(true);
  if(personalTradeId!==null || !$('pe_active').hidden) throw new Error('logout leaked a position');
  if($('pe_entry_input').value!=='' || $('pe_exit_input').value!=='') throw new Error('logout leaked filled rates');
})().catch(e=>{console.error(e);process.exitCode=1;});
"""
    path=tmp_path/"flexible_dashboard.js"
    path.write_text(harness)
    proc=subprocess.run(["node",str(path)],capture_output=True,text=True)
    assert proc.returncode==0,proc.stderr
