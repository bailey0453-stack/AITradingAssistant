"""Both directions, cost asymmetry, forecast threshold, and retained WAIT gates."""
import pytest

from app.services.five_cent_plan import apply


@pytest.mark.parametrize("direction,expected,target,stop,entry", [
    ("BUY_USD", 18.60, 18.55, 18.45, 18.501),
    ("SELL_USD", 18.40, 18.45, 18.55, 18.499),
])
def test_five_cent_levels_and_net_costs(direction,expected,target,stop,entry):
    out=apply({"action":"TRADE","expected_rate":expected,"forecast_for":"deadline","wait_reasons":[]},
              direction,{"bid":18.499,"ask":18.501})
    plan=out["five_cent_plan"]
    assert plan["target_rate"]==target and plan["stop_rate"]==stop
    assert plan["entry_rate"]==entry and plan["exit_deadline"]=="deadline"
    assert plan["target_net_pnl_usd"]>0
    assert -plan["stop_net_pnl_usd"]>plan["target_net_pnl_usd"]
    assert plan["status"]=="signal"
    assert plan["target_before_stop_probability"] is None


@pytest.mark.parametrize("direction,expected",[("BUY_USD",18.52),("SELL_USD",18.48)])
def test_small_forecast_does_not_become_five_cent_prediction(direction,expected):
    out=apply({"action":"TRADE","expected_rate":expected,"wait_reasons":[]},direction,{"bid":18.499,"ask":18.501})
    assert out["action"]=="WAIT"
    assert out["expected_rate"]==expected
    assert any("smaller" in r for r in out["wait_reasons"])
    assert out["five_cent_plan"]["status"]=="conditional"


def test_wide_spread_blocks_five_cent_setup_even_with_larger_forecast():
    out=apply({"action":"TRADE","expected_rate":18.7,"wait_reasons":[]},"BUY_USD",{"bid":18.47,"ask":18.53})
    assert out["action"]=="WAIT"
    assert out["five_cent_plan"]["target_net_pnl_usd"]<0
    assert any("does not cover" in r for r in out["wait_reasons"])


def test_existing_wait_is_retained_and_missing_evidence_has_no_levels():
    out=apply({"action":"WAIT","expected_rate":18.6,"wait_reasons":["News conflict"]},"BUY_USD",{"bid":18.499,"ask":18.501})
    assert out["action"]=="WAIT" and out["five_cent_plan"]["status"]=="conditional"
    out=apply({"action":"WAIT","expected_rate":None},None,{"bid":18.499,"ask":18.501})
    assert out["five_cent_plan"]["target_rate"] is None
    assert out["five_cent_plan"]["stop_rate"] is None
