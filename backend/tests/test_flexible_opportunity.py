"""First-touch ordering, flexible timing, purged evidence, and live-data limits."""
from datetime import datetime,timedelta,timezone
from unittest.mock import patch

import pytest

from app.services import flexible_opportunity as fo

NOW = datetime(2026,11,17,18,tzinfo=timezone.utc)


def fixture(slope=.0003):
    points=[(NOW-timedelta(minutes=30000-m),20+slope*(m-30000)) for m in range(30001)]
    fix={"bid":19.9999,"ask":20.0001,"updated_at":NOW.isoformat()}
    payload={"market_state":{"is_open":True},"context":{"upcoming_events":[]},
             "data_sources":{"calendar":"live","calendar_status":"LIVE"},
             "news_watch":{"status":"LIVE_POLL","checked_at":NOW.isoformat(),"items":[]}}
    return points,fix,payload


@pytest.mark.parametrize("direction,path,kind",[
    ("BUY_USD",[(1,.06),(4,-.10)],"target"),
    ("BUY_USD",[(1,-.06),(4,.10)],"stop"),
    ("SELL_USD",[(1,-.06),(4,.10)],"target"),
    ("SELL_USD",[(1,.06),(4,-.10)],"stop"),
    ("BUY_USD",[(30,.01),(300,.02)],"timeout"),
])
def test_first_touch_not_final_direction(direction,path,kind):
    result=fo.outcome({"path":path},direction,{"bid":18.4999,"ask":18.5001})
    assert result["kind"]==kind
    if kind=="stop": assert result["net"]<0
    if kind=="target": assert result["net"]>0


def test_spread_is_paid_and_stop_overshoot_is_retained():
    path={"path":[(1,-.07),(300,.20)]}
    narrow=fo.outcome(path,"BUY_USD",{"bid":18.4999,"ask":18.5001})
    wide=fo.outcome(path,"BUY_USD",{"bid":18.49,"ask":18.51})
    assert narrow["kind"]==wide["kind"]=="stop"
    assert wide["net"]<narrow["net"]<fo.net_pnl("BUY_USD",18.5001,18.4501)


@pytest.mark.parametrize("slope,direction",[(.0003,"BUY_USD"),(-.0003,"SELL_USD")])
def test_five_cent_opportunity_can_take_more_than_twenty_minutes(slope,direction):
    out=fo.build(*fixture(slope),now=NOW)
    assert out["direction"]==direction
    assert out["action"]!="WAIT",out["wait_reasons"]
    assert out["max_hold_minutes"]==300
    assert 20<out["timing"]["median_minutes"]<=300
    sign=1 if direction=="BUY_USD" else -1
    assert sign*(out["setup"]["target_rate"]-out["setup"]["entry_rate"])==pytest.approx(.05)
    assert out["validation"]["samples"]>=fo.MIN_VALIDATION
    assert out["confidence"] is None


def test_sparse_or_future_history_cannot_inflate_first_touch_evidence():
    points,fix,payload=fixture()
    out=fo.build(points[::5],fix,payload,now=NOW)
    assert out["action"]=="WAIT" and out["analogs"]==0
    original=fo.build(points,fix,payload,now=NOW)
    assert original==fo.build(points*2+[(NOW+timedelta(minutes=1),100)],fix,payload,now=NOW)


def test_walk_forward_training_ends_before_test_lookback():
    points,fix,_=fixture()
    rows=fo.windows(points)
    calls=[]
    original=fo.neighbors
    def capture(training,features):
        calls.append(training)
        return original(training,features)
    with patch.object(fo,"neighbors",side_effect=capture):
        fo.validate(rows,"BUY_USD",fix)
    for training,test in zip(calls,rows[max(fo.MIN_ANALOGS,len(rows)-120):]):
        assert all(r["end"]<test["at"]-fo.LOOKBACK for r in training)
    assert all(b["at"]-fo.LOOKBACK>a["end"] for a,b in zip(rows,rows[1:]))


@pytest.mark.parametrize("risk",["quote","calendar","event","news","closed","weak_validation"])
def test_risk_and_evidence_gates_are_preserved(risk):
    points,fix,payload=fixture()
    if risk=="quote": fix["updated_at"]=(NOW-timedelta(seconds=31)).isoformat()
    if risk=="calendar": payload["data_sources"]["calendar_status"]="PARTIAL"
    if risk=="closed": payload["market_state"]["is_open"]=False
    if risk=="news": payload["news_watch"]["status"]="UNAVAILABLE"
    if risk=="event": payload["context"]["upcoming_events"]=[{"importance":"high","title":"CPI","release_time":(NOW+timedelta(minutes=5)).isoformat()}]
    if risk=="weak_validation":
        with patch.object(fo,"validate",return_value={"samples":2,"average_net_lower_99_usd":100,"target_first_lower_95_pct":99}):
            out=fo.build(points,fix,payload,now=NOW)
    else: out=fo.build(points,fix,payload,now=NOW)
    assert out["action"]=="WAIT" and out["wait_reasons"]


def test_an_event_two_hours_away_does_not_impose_old_four_hour_block():
    points,fix,payload=fixture()
    payload["context"]["upcoming_events"]=[{"importance":"high","title":"CPI","release_time":(NOW+timedelta(hours=2)).isoformat()}]
    assert fo.build(points,fix,payload,now=NOW)["action"]!="WAIT"


def test_timeout_gains_do_not_masquerade_as_five_cent_target_hits():
    stats=fo.summarize([{"kind":"timeout","minutes":300,"net":100}]*40)
    assert stats["average_net_usd"]==100 and stats["target_first_pct"]==0
    assert stats["target_first_lower_95_pct"]==0
    assert stats["target_hit_minutes_median"] is None
