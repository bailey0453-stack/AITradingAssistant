"""Frozen consensus, publication timing, unit identity, and live-data boundaries."""
from datetime import datetime,timedelta,timezone
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from sqlalchemy import create_engine,select
from sqlalchemy.orm import Session

from app.config import Settings
from app.models.release_watch import ReleaseWatch
from app.services import release_watch as rw

AT=datetime(2026,11,17,13,30,tzinfo=timezone.utc)


def event(**changes):
    row={"event_key":"retail-2026-10","event_name":"Retail Sales MoM","series":"us_retail_mom_headline",
         "country":"US","unit":"%","reference":"Oct","release_time":AT,"source":"finnhub",
         "consensus":.2,"actual":None,"previous":.8,"polarity":1}
    row.update(changes)
    return row


def quote(now,mid=18):
    return {"bid":mid-.0001,"ask":mid+.0001,"updated_at":now.isoformat()}


def record(at=AT,**changes):
    d={"event_key":str(at),"event_name":"Retail Sales MoM","series":"us_retail_mom_headline",
       "country":"US","unit":"%","reference":"Oct","release_time":at,"source":"finnhub",
       "consensus":.2,"consensus_seen_at":at-timedelta(seconds=20),"previous":.8,
       "actual":.8,"latest_actual":.8,"actual_seen_at":at+timedelta(seconds=5),"revised_previous":.8,
       "pre_bid":17.9999,"pre_ask":18.0001,"pre_quote_at":at-timedelta(seconds=20),
       "outcome_20m":None,"outcome_at":None}
    d.update(changes)
    return SimpleNamespace(**d)


def data(now,events=()):
    return {"status":"LIVE_POLL","provider":"finnhub","checked_at":now.isoformat(),"events":events,"poll_seconds":5}


@pytest.mark.parametrize("value,expected", [("0%",0),("-0.2%",-.2),("−0.2%",-.2),("178K",178000),("1,000",1000),(".3",.3),("NaN",None),("Infinity",None),(True,None)])
def test_numeric_parser_preserves_zero_and_units(value,expected):
    assert rw.number(value)==expected


def test_provider_maps_consensus_separately_from_own_forecast():
    row={"Event":"Retail Sales MoM","Country":"United States","Date":AT.isoformat(),"Unit":"%","TEForecast":".8%","Forecast":None,"Actual":None}
    assert rw._map(row,"tradingeconomics")["consensus"] is None
    row["Forecast"]="0%"
    assert rw._map(row,"tradingeconomics")["consensus"]==0
    row["DateSpan"]="1"
    assert rw._map(row,"tradingeconomics") is None


def test_series_identity_does_not_mix_yoy_mom_core_or_control():
    assert rw.identify("Retail Sales YoY","US","%") is None
    names=["Retail Sales MoM","Retail Sales Ex Autos MoM","Retail Sales Ex Gas Autos MoM","Retail Sales Control Group"]
    assert len({rw.identify(n,"US","%")[0] for n in names})==4
    assert rw.identify("Core CPI MoM","US","%")[0]!=rw.identify("CPI MoM","US","%")[0]
    assert rw.identify("Unemployment Rate","US","%")[1]==-1


def test_count_units_are_not_mixed_with_percentage_points():
    row={"event":"Nonfarm Payrolls","country":"US","time":AT.isoformat(),"unit":"K","estimate":175,"actual":"180K"}
    mapped=rw._map(row,"finnhub")
    assert mapped["consensus"]==175000
    assert mapped["actual"]==180000


def test_consensus_and_initial_actual_are_frozen_and_outcome_is_exact_twenty_minutes():
    engine=create_engine("sqlite://")
    ReleaseWatch.__table__.create(engine)
    with Session(engine) as db:
        before=AT-timedelta(seconds=20)
        rw.capture(db,data(before,[event()]),quote(before),now=before)
        after=AT+timedelta(seconds=5)
        rw.capture(db,data(after,[event(consensus=.9,actual=.7)]),quote(after,18.01),now=after)
        row=db.execute(select(ReleaseWatch)).scalar_one()
        assert row.consensus==.2
        assert rw.aware(row.consensus_seen_at)==before
        assert row.actual==.7
        later=AT+timedelta(seconds=20)
        rw.capture(db,data(later,[event(consensus=1,actual=.6)]),quote(later,18.02),now=later)
        assert row.actual==.7 and row.latest_actual==.6
        end=AT+timedelta(minutes=20)
        rw.capture(db,data(end,[event(actual=.6)]),quote(end,18.04),now=end)
        assert row.outcome_20m==pytest.approx(18.04/18-1)


def test_first_seen_after_release_cannot_reconstruct_pre_release_consensus():
    engine=create_engine("sqlite://")
    ReleaseWatch.__table__.create(engine)
    now=AT+timedelta(seconds=10)
    with Session(engine) as db:
        rows=rw.capture(db,data(now,[event(actual=.7)]),quote(now),now=now)
        assert rows[0].consensus is None
        out=rw.assess(rows,data(now),quote(now),now=now)
        assert out["action"]=="WAIT"
        assert any("Consensus was not captured" in r for r in out["wait_reasons"])


def test_pre_release_has_conditional_scenarios_and_no_trade_or_target():
    now=AT-timedelta(minutes=2)
    row=record(actual=None,latest_actual=None,actual_seen_at=None,consensus_seen_at=AT-timedelta(minutes=5))
    out=rw.assess([row],data(now),quote(now),now=now)
    assert out["phase"]=="PRE_RELEASE" and out["action"]=="WAIT"
    assert out["expected_rate"] is None
    assert "upward" in out["scenarios"][0]["above_consensus"]


def test_result_with_price_confirmation_does_not_invent_target_or_confidence():
    now=AT+timedelta(seconds=20)
    out=rw.assess([record()],data(now),quote(now,18.01),now=now)
    assert out["phase"]=="REACTION_CONFIRMED"
    assert out["surprise_direction"]=="BUY_USD"
    assert out["expected_rate"] is None and out["confidence"] is None
    assert out["action"]=="WAIT"


@pytest.mark.parametrize("change", [{"actual":None},{"actual_seen_at":AT+timedelta(seconds=90)}, {"latest_actual":.9},{"revised_previous":.1},{"pre_quote_at":None}])
def test_missing_or_late_or_revised_inputs_do_not_issue_action(change):
    now=AT+timedelta(seconds=100)
    out=rw.assess([record(**change)],data(now),quote(now,18.01),now=now)
    assert out["action"]=="WAIT" and out["wait_reasons"]


def test_conflicting_simultaneous_releases_block_action():
    now=AT+timedelta(seconds=20)
    other=record(event_key="imports",event_name="Import Prices MoM",series="us_import_prices_mom",actual=.1,latest_actual=.1)
    out=rw.assess([record(),other],data(now),quote(now,18.01),now=now)
    assert any("conflict" in r for r in out["wait_reasons"])
    assert out["expected_rate"] is None


def test_model_uses_only_genuine_completed_same_report_outcomes():
    now=AT+timedelta(seconds=20)
    history=[record(AT-timedelta(days=31*(i+1)),outcome_20m=.002,outcome_at=AT-timedelta(days=31*(i+1))+timedelta(minutes=20)) for i in range(10)]
    out=rw.assess([record()],data(now),quote(now,18.01),history=history,now=now)
    assert out["action"]=="WAIT"  # 2.6 cents left is short of the five-cent target.
    assert out["expected_rate"]==18.036
    assert out["history_samples"]==10 and out["confidence"] is None
    assert out["five_cent_plan"]["target_rate"]==18.06
    for row in history: row.outcome_20m=.004
    out=rw.assess([record()],data(now),quote(now,18.01),history=history,now=now)
    assert out["action"]=="BUY USD / SELL MXN NOW"
    assert out["five_cent_plan"]["status"]=="signal"
    history[0].source="sample"
    history[1].consensus_seen_at=history[1].release_time+timedelta(seconds=1)
    history[2].outcome_at=AT+timedelta(minutes=20)
    out=rw.assess([record()],data(now),quote(now,18.01),history=history,now=now)
    assert out["history_samples"]==7 and out["action"]=="WAIT"


def test_provider_permission_failure_is_explicit_and_never_mock():
    class Response:
        status_code=403
    rw._cache=None
    try:
        with patch.object(rw.httpx,"get",return_value=Response()) as request:
            out=rw.feed(settings=Settings(use_mock_data=False,news_provider="finnhub",news_api_key="test-secret"),now=AT)
        assert out["status"]=="ACCESS_REQUIRED" and out["events"]==[]
        assert request.call_args.kwargs["headers"]["X-Finnhub-Token"]=="test-secret"
        assert "test-secret" not in str(out)
    finally: rw._cache=None


def test_current_output_has_no_result_at_future_release():
    now=AT-timedelta(seconds=5)
    out=rw.assess([record()],data(now),quote(now),now=now)
    assert out["phase"]=="PRE_RELEASE"
    assert out["action"]=="WAIT" and out["expected_rate"] is None
    assert out["events"][0]["actual"] is None


def test_premature_actual_is_not_ingested_as_a_released_result():
    engine=create_engine("sqlite://")
    ReleaseWatch.__table__.create(engine)
    before=AT-timedelta(seconds=5)
    with Session(engine) as db:
        rows=rw.capture(db,data(before,[event(actual=.7)]),quote(before),now=before)
        assert rows[0].actual is None
        after=AT+timedelta(seconds=1)
        rows=rw.capture(db,data(before,[event(actual=.7)]),quote(after),now=after)
        assert rows[0].actual is None
