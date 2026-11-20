from datetime import datetime, timedelta, timezone
import math

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.database import Base
from app.models.rebound import ReboundEvent
from app.services.dip_rebound import Detector, net, replay
from app.services.rebound_watch import message, persist

START = datetime(2026,11,20,14,0,tzinfo=timezone.utc)


def quote(at, bid, spread=.004):
    return dict(symbol='USDMXN.r',source='centroid_fix',bid=bid,ask=bid+spread,updated_at=at.isoformat())


def path(spread=.004):
    # 20 minutes of actual-looking pre-dip observations, then a falling leg,
    # low, bounce, higher-low pullback and completed swing-high breakout.
    bars = [(19.10,19.10,19.10,19.10)]*20 + [
        (19.08,19.08,19.04,19.04), (19.02,19.02,18.98,18.98),
        (18.97,18.975,18.96,18.965), (18.98,18.995,18.98,18.99),
        (18.998,19.005,18.995,19.0), (18.996,19.0,18.99,18.995),
        (18.997,19.01,18.997,19.009)]
    rows=[]
    for minute, values in enumerate(bars):
        for step in range(12):
            rows.append(quote(START+timedelta(minutes=minute,seconds=step*5),values[min(step//3,3)],spread))
    rows.append(quote(START+timedelta(minutes=len(bars)),19.009,spread))
    return rows


def feed(rows):
    d,events=Detector(),[]
    for q in rows:
        events += d.observe(q,datetime.fromisoformat(q['updated_at']))
    return d,events


def test_causal_higher_low_entry_with_executable_costs_and_fixed_levels():
    rows=path()
    d,events=feed(rows[:-1])
    assert not any(e['stage']=='ENTRY' for e in events)
    events += d.observe(rows[-1],START+timedelta(minutes=27))
    entry=next(e for e in events if e['stage']=='ENTRY')
    assert entry['entry_ask']==pytest.approx(19.013)
    assert entry['target_bid']==pytest.approx(19.073)
    assert entry['stop_bid']==pytest.approx(18.987)
    assert entry['net_reward_risk']>=1.5
    assert entry['target_net_usd']==net(19.013,19.073)
    assert 'PAPER' in message(entry) and 'No order sent' in message(entry)
    frozen=dict(d.plan)
    at=START+timedelta(minutes=27,seconds=5)
    assert not d.observe(quote(at,19.03),at)
    assert d.plan==frozen
    at+=timedelta(seconds=5)
    outcome=d.observe(quote(at,19.08),at)[0]
    assert outcome['stage']=='TARGET'
    assert outcome['realized_paper_net_usd']==net(19.013,19.08)
    assert d.plan is None


def test_no_entry_from_a_continuing_fall_or_without_higher_low():
    rows=path()[:23*12]
    d,events=feed(rows)
    assert any(e['stage']=='DIP' for e in events)
    assert not any(e['stage']=='ENTRY' for e in events)
    for step in range(60):
        at=START+timedelta(minutes=23,seconds=step*5)
        assert not any(e['stage']=='ENTRY' for e in d.observe(quote(at,18.95-step*.0005),at))


def test_wide_spread_and_late_entry_fail_cost_filters():
    d,events=feed(path(.02))
    assert not any(e['stage']=='ENTRY' for e in events)
    assert d.status=='SKIP COST / RISK'
    rows=path();rows[-1]=quote(START+timedelta(minutes=27),19.07)
    d,events=feed(rows)
    assert not any(e['stage']=='ENTRY' for e in events)


def test_stop_overshoot_and_timeout_are_losses_or_actual_mark_not_target():
    d,_=feed(path())
    at=START+timedelta(minutes=27,seconds=5)
    event=d.observe(quote(at,18.97),at)[0]
    assert event['stage']=='STOP'
    assert event['realized_paper_net_usd']==net(19.013,18.97)
    d,_=feed(path())
    d.plan['deadline']=at.isoformat()
    assert d.observe(quote(at,19.01),at)[0]['stage']=='TIMEOUT'


def test_gap_invalidates_paper_plan_without_inventing_outcome():
    d,_=feed(path())
    at=START+timedelta(minutes=28)
    events=d.observe(quote(at,19.10),at)
    assert [e['stage'] for e in events]==['COVERAGE_GAP']
    assert d.plan is None and len(d.bars)==0
    assert d.snapshot(at+timedelta(seconds=11))['setup'] is None


@pytest.mark.parametrize('change',[
    {'bid':math.nan},{'ask':math.inf},{'bid':0},{'bid':20,'ask':19},
    {'source':'mock'},{'symbol':'EURUSD'},
    {'updated_at':(START-timedelta(seconds=11)).isoformat()},
    {'updated_at':(START+timedelta(seconds=3)).isoformat()},
])
def test_bad_or_stale_data_never_creates_signal(change):
    d=Detector()
    assert not d.observe({**quote(START,19),**change},START)
    assert d.last_at is None


def test_duplicate_and_out_of_order_quotes_do_not_manufacture_candles():
    d=Detector();q=quote(START,19)
    d.observe(q,START)
    for _ in range(100):
        assert not d.observe(q,START)
    assert not d.bars


def test_replay_unfinished_and_durable_event_deduplication():
    result=replay(path())
    assert result['unfinished'] and result['completed']==0 and result['net_usd']==0
    engine=create_engine('sqlite://')
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        assert persist(db,result['events'])
        assert persist(db,result['events'])==[]
        assert len(db.scalars(select(ReboundEvent)).all())==len(result['events'])


def test_worker_is_off_by_default_and_no_serverless_monitor(monkeypatch):
    from app.config import Settings
    from app.services import rebound_watch as worker
    settings=Settings(_env_file=None)
    assert not settings.rebound_paper_enabled and not settings.rebound_paper_alerts
    monkeypatch.setattr(worker,'get_settings',lambda:settings)
    worker.start()
    assert worker._thread is None
    settings.rebound_paper_enabled=True
    monkeypatch.setenv('VERCEL','1')
    worker.start()
    assert worker._thread is None


def test_remote_stale_plan_is_not_displayed_as_current(monkeypatch):
    from app.services import rebound_watch as worker
    from app.services.fix import provider
    monkeypatch.setenv('VERCEL','1')
    monkeypatch.setattr(provider,'_remote_worker_request',lambda *a,**k:{
        'status':'PAPER BUY SETUP','quote_at':START.isoformat(),'setup':{'entry_ask':19}})
    out=worker.current(None)
    assert out['status']=='DATA GAP' and out['setup'] is None


def test_local_and_http_status_report_disabled_without_fabricated_plan():
    from fastapi.testclient import TestClient
    from app.database import get_db
    from app.main import app
    from app.services.rebound_watch import local_status
    engine=create_engine('sqlite://',connect_args={'check_same_thread':False},
                         poolclass=__import__('sqlalchemy').pool.StaticPool)
    Base.metadata.create_all(engine)
    def db_session():
        with Session(engine) as db:
            yield db
    app.dependency_overrides[get_db]=db_session
    try:
        with Session(engine) as db:
            assert local_status(db)['status']=='DISABLED'
        response=TestClient(app).get('/decision/rebound')
        assert response.status_code==200
        assert response.json()['setup'] is None
        assert not response.json()['live_execution']
    finally:
        app.dependency_overrides.pop(get_db,None)
