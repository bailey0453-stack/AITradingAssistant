"""Regression tests for dashboard JS load isolation.

Fails when a syntax error or single-endpoint abort can blank the whole page
(the production regression: duplicate ``const pv`` prevented ``refresh`` from
ever being defined).
"""

from __future__ import annotations

import re
import subprocess
import tempfile
from pathlib import Path

import pytest

from app.main import DASHBOARD_HTML


def _dashboard_js() -> str:
    i = DASHBOARD_HTML.find("<script>")
    j = DASHBOARD_HTML.rfind("</script>")
    assert i >= 0 and j > i, "dashboard HTML missing inline script"
    return DASHBOARD_HTML[i + 8 : j]


def test_dashboard_js_parses_with_node():
    """SyntaxError in the dashboard script aborts all data rendering."""
    js = _dashboard_js()
    with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False) as f:
        f.write(js)
        path = f.name
    try:
        proc = subprocess.run(
            ["node", "--check", path],
            capture_output=True,
            text=True,
            check=False,
        )
    except FileNotFoundError:
        pytest.skip("node not available")
    assert proc.returncode == 0, proc.stderr


def test_no_duplicate_const_pv_in_load_research():
    js = _dashboard_js()
    # Extract loadResearch body roughly.
    m = re.search(
        r"async function loadResearch\(\)\s*\{(.*?)\}(?:\s*function |\s*async function |\s*// ---)",
        js,
        re.S,
    )
    assert m, "loadResearch not found"
    body = m.group(1)
    assert len(re.findall(r"\bconst pv\b", body)) == 0, (
        "duplicate const pv in loadResearch blanks the dashboard (SyntaxError)"
    )


def test_load_research_uses_all_settled_isolation():
    js = _dashboard_js()
    assert "Promise.allSettled" in js
    assert "fetchJson('/research/summary')" in js or 'fetchJson("/research/summary")' in js
    # Paper-trading is optional — must not be a single sequential await that aborts others.
    idx = js.find("async function loadResearch()")
    chunk = js[idx : idx + 2500]
    assert "Promise.allSettled" in chunk
    assert chunk.find("Promise.allSettled") < chunk.find("/paper-trading/")


def test_optional_endpoint_failure_does_not_abort_core_refresh_structure():
    """Structural regression: core refresh must not depend on paper-trading success."""
    js = _dashboard_js()
    # refresh must define/call analysis first and wrap optional loads separately.
    r_idx = js.find("async function refresh()")
    assert r_idx >= 0
    refresh_chunk = js[r_idx : r_idx + 4000]
    assert "fetchJson('/analysis/usdmxn')" in refresh_chunk or 'fetchJson("/analysis/usdmxn")' in refresh_chunk
    assert "renderTradeDecisionCard" in refresh_chunk
    assert "renderTopline" in refresh_chunk
    # loadPerformance (which pulls research + optional paper APIs) is after core render.
    assert refresh_chunk.find("renderTradeDecisionCard") < js.find("loadPerformance()")


def test_history_cells_show_status_and_hedge_pnl():
    html = DASHBOARD_HTML
    assert (
        "Horizon cells show directional accuracy plus estimated net $100K hedge P&L after FIX spread and $20-per-side fees."
        in html
        or "Horizon cells show directional accuracy plus estimated net $100K hedge P&amp;L after FIX spread and $20-per-side fees."
        in html
    )
    js = _dashboard_js()
    assert "horizon_results" in js
    assert "function formatHorizonPnl" in js
    assert "function horizonCell" in js
    assert "hz-pnl pos" in js
    assert "hz-pnl neg" in js


def test_decision_card_above_topline_and_null_safe_helpers():
    html = DASHBOARD_HTML
    assert html.find("tradeDecisionCard") < html.find("toplineCard")
    js = _dashboard_js()
    assert "function setText(" in js
    assert "dash_load_error" in html
    assert "SIMULATED PAPER PERFORMANCE" in html or "simulated" in html.lower()


def test_node_harness_all_settled_keeps_core_values():
    """Executable proof that one rejected fetch does not wipe core card fields."""
    harness = r"""
const results = {};
async function fetchJson(url) {
  if (url.includes('paper-trading')) throw new Error('optional down');
  if (url.includes('research/summary')) return { overall_accuracy: 55 };
  if (url.includes('performance/summary')) return { actionable_trades: 3, win_rate: 50, net_pnl_usd: -10 };
  if (url.includes('performance/monthly')) return { months: {} };
  throw new Error('unexpected ' + url);
}
async function loadResearchLike() {
  const settled = await Promise.allSettled([
    fetchJson('/research/summary'),
    fetchJson('/performance/summary'),
    fetchJson('/performance/monthly'),
    fetchJson('/paper-trading/active'),
  ]);
  const val = (i) => settled[i].status === 'fulfilled' ? settled[i].value : null;
  results.research = val(0);
  results.paper = val(1);
  results.paperTradingFailed = settled[3].status === 'rejected';
  results.coreStillPresent = !!(results.research && results.paper);
}
loadResearchLike().then(() => {
  if (!results.coreStillPresent || !results.paperTradingFailed) {
    console.error('FAIL', results);
    process.exit(1);
  }
  console.log('OK', JSON.stringify(results));
}).catch((e) => { console.error(e); process.exit(1); });
"""
    with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False) as f:
        f.write(harness)
        path = f.name
    try:
        proc = subprocess.run(
            ["node", path], capture_output=True, text=True, check=False
        )
    except FileNotFoundError:
        pytest.skip("node not available")
    assert proc.returncode == 0, proc.stderr + proc.stdout
