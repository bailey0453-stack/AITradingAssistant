"""Persistent levels, both executable exit sides, lost data and exit priorities."""
from datetime import datetime,timedelta,timezone
from types import SimpleNamespace

import pytest

from app.services import personal_exit_watch as watch

NOW=datetime(2026,11,17,18,tzinfo=timezone.utc)


def trade(direction="BUY_USD",**changes):
    sign=1 if direction=="BUY_USD" else -1
    data=dict(id="trade-1",direction=direction,entry_rate=18.5,target_rate=18.5+sign*.05,stop_rate=18.5-sign*.05,
              opened_at=NOW-timedelta(minutes=10),review_at=NOW+timedelta(hours=4,minutes=50),
              peak_rate=18.5,last_quote_at=NOW-timedelta(minutes=1),closed_at=None,exit_rate=None,coverage_gap=False,
              exit_advice=None,advice_reason=None,advice_at=None)
    return SimpleNamespace(**(data|changes))


def fix(bid,ask=None,at=NOW):
    return {"bid":bid,"ask":ask if ask is not None else bid+.0002,"updated_at":at.isoformat()}


def payload():
    return {"market_state":{"is_open":True},"context":{"upcoming_events":[]},
            "news_watch":{"status":"LIVE_POLL","checked_at":NOW.isoformat(),"items":[]}}


@pytest.mark.parametrize("direction,quote,action",[
    ("BUY_USD",fix(18.55),"TAKE PROFIT"),("BUY_USD",fix(18.4498),"EXIT / STOP"),
    ("SELL_USD",fix(18.4498,18.45),"TAKE PROFIT"),("SELL_USD",fix(18.5498,18.55),"EXIT / STOP"),
])
def test_uses_correct_exit_side_and_original_levels(direction,quote,action):
    p=trade(direction)
    original=(p.entry_rate,p.target_rate,p.stop_rate,p.opened_at,p.review_at)
    out=watch.assess(p,quote,[],payload(),now=NOW)
    assert out["action"]==action
    assert (p.entry_rate,p.target_rate,p.stop_rate,p.opened_at,p.review_at)==original
    assert p.closed_at is None


def test_midpoint_touch_does_not_count_when_executable_bid_has_not_reached_target():
    p=trade()
    out=watch.assess(p,fix(18.549,18.551),[],payload(),now=NOW)
    assert out["action"]!="TAKE PROFIT" and p.exit_advice is None


def test_target_or_stop_advice_is_latched_without_claiming_trade_closed():
    p=trade(last_quote_at=NOW-timedelta(minutes=2))
    old=NOW-timedelta(minutes=1)
    out=watch.assess(p,fix(18.51),[],payload(),observations=[(old,18.44,18.4402)],now=NOW)
    assert out["action"]=="EXIT / STOP" and out["advice_at"]==old.isoformat()
    again=watch.assess(p,None,[],payload(),now=NOW+timedelta(seconds=40))
    assert again["action"]=="EXIT / STOP" and again["current_exit_rate"] is None
    assert p.closed_at is None


@pytest.mark.parametrize("quote",[None,fix(18.51,at=NOW-timedelta(seconds=31)),fix(float("nan"))])
def test_stale_or_invalid_quote_never_produces_hold_or_numeric_pnl(quote):
    out=watch.assess(trade(),quote,[],payload(),now=NOW)
    assert out["action"]=="CHECK LIVE QUOTE"
    assert out["net_pnl_usd"] is None and out["current_exit_rate"] is None
    assert out["target_rate"]==18.55


@pytest.mark.parametrize("direction,peak,quote",[
    ("BUY_USD",18.54,fix(18.525)),("SELL_USD",18.46,fix(18.4748,18.475))])
def test_profit_giveback_suggests_early_exit_without_moving_stop(direction,peak,quote):
    p=trade(direction,peak_rate=peak)
    stop=p.stop_rate
    out=watch.assess(p,quote,[],payload(),now=NOW)
    assert out["action"]=="TAKE PROFIT EARLY"
    assert p.stop_rate==stop and p.closed_at is None


def test_price_reversal_and_five_hour_review_are_explicit():
    p=trade()
    prices=[(NOW-timedelta(minutes=15),18.54),(NOW-timedelta(minutes=5),18.535)]
    out=watch.assess(p,fix(18.52),prices,payload(),now=NOW)
    assert out["action"]=="EXIT EARLY"
    p=trade(review_at=NOW-timedelta(seconds=1))
    out=watch.assess(p,fix(18.51),[],payload(),now=NOW)
    assert out["action"]=="REASSESS" and "Five hours" in out["reason"]


def test_giveback_is_not_called_profit_when_fees_exceed_remaining_gain():
    out=watch.assess(trade(peak_rate=18.54),fix(18.501),[],payload(),now=NOW)
    assert out["net_pnl_usd"]<0 and out["action"]=="EXIT EARLY"


def test_missing_news_does_not_invent_exit_but_prevents_supported_hold():
    prices=[(NOW-timedelta(minutes=15),18.49),(NOW-timedelta(minutes=5),18.50)]
    out=watch.assess(trade(),fix(18.51),prices,payload(),now=NOW)
    assert out["action"]=="HOLD"
    data=payload();data["news_watch"]["status"]="UNAVAILABLE"
    out=watch.assess(trade(),fix(18.51),prices,data,now=NOW)
    assert out["action"]=="REASSESS"


def test_quotes_before_entry_and_future_quotes_cannot_trigger_exit():
    p=trade()
    seen=[(p.opened_at-timedelta(seconds=1),18.56,18.5602),(NOW+timedelta(minutes=1),18.44,18.4402)]
    out=watch.assess(p,fix(18.51),[],payload(),observations=seen,now=NOW)
    assert p.exit_advice is None and out["action"]!="TAKE PROFIT"


def test_observation_gaps_are_reported():
    p=trade(last_quote_at=None)
    out=watch.assess(p,fix(18.51),[],payload(),now=NOW)
    assert any("gap" in r for r in out["warnings"])
    again=watch.assess(p,fix(18.51,at=NOW+timedelta(seconds=10)),[],payload(),now=NOW+timedelta(seconds=10))
    assert any("gap" in r for r in again["warnings"])
