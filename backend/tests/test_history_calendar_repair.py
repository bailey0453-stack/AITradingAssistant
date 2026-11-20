from datetime import datetime,timedelta,timezone
from types import SimpleNamespace

import httpx
import pytest
from sqlalchemy import create_engine,select
from sqlalchemy.orm import Session,sessionmaker

from app.config import Settings
from app.database import Base
from app.models import ObservedFixQuote
from app.services import fix_history_recorder as recorder
from app.services import official_calendar as cal
from app.services.flexible_opportunity import upcoming_risk
from app.services.fx_observations import capture


def response(data):
    return httpx.Response(200,json=data,request=httpx.Request('GET','https://example.invalid'))


@pytest.fixture
def history(monkeypatch):
    engine=create_engine('sqlite://')
    Base.metadata.create_all(engine)
    monkeypatch.setattr(recorder,'SessionLocal',sessionmaker(engine))
    monkeypatch.setattr(recorder,'_last_minute',None)
    monkeypatch.setattr(recorder,'_error',None)
    yield engine
    engine.dispose()


def test_history_persists_with_no_dashboard_requests_and_deduplicates(history):
    now=datetime(2026,11,20,15,tzinfo=timezone.utc)
    for minute in range(3):
        at=now+timedelta(minutes=minute)
        q=dict(symbol='USDMXN.r',source='centroid_fix',bid=19.0,ask=19.01,updated_at=at.isoformat())
        assert recorder.sample(q,at)
        assert not recorder.sample(q,at)
    with Session(history) as db:
        rows=db.scalars(select(ObservedFixQuote).order_by(ObservedFixQuote.quote_at)).all()
        assert len(rows)==3 and rows[-1].bid==19.0


@pytest.mark.parametrize('change',[{'source':'mock'},{'source':'other'},{'symbol':'EURUSD'},
    {'source':None},{'ask':18.0},{'updated_at':'bad'}])
def test_recorder_and_dashboard_capture_reject_bad_provenance(history,change):
    now=datetime.now(timezone.utc)
    q={**dict(symbol='USDMXN.r',source='centroid_fix',bid=19,ask=19.01,updated_at=now.isoformat()),**change}
    assert not recorder.sample(q,now)
    with Session(history) as db:
        assert not capture(db,q,now)
        db.commit()
        assert not db.scalars(select(ObservedFixQuote)).all()


def test_persistence_failure_retries_same_minute(history,monkeypatch):
    now=datetime.now(timezone.utc)
    q=dict(symbol='USDMXN.r',source='centroid_fix',bid=19,ask=19.01,updated_at=now.isoformat())
    factory=recorder.SessionLocal
    monkeypatch.setattr(recorder,'SessionLocal',lambda:(_ for _ in ()).throw(RuntimeError('offline')))
    with pytest.raises(RuntimeError):recorder.sample(q,now)
    assert recorder._last_minute is None
    monkeypatch.setattr(recorder,'SessionLocal',factory)
    assert recorder.sample(q,now)


def test_treasury_uses_published_holiday_time_and_date_only_risk(monkeypatch):
    rows=[dict(competitiveAuctionCloseDate='2026-11-25T00:00:00',competitiveAuctionCloseTime='11:30 AM',securityType='Note',securityTerm='7-Year'),
          dict(competitiveAuctionCloseDate='2026-11-25T00:00:00',competitiveAuctionCloseTime='',securityType='Bill',securityTerm='17-Week')]
    monkeypatch.setattr(cal.httpx,'get',lambda *a,**k:response(rows))
    events=cal.TreasuryAuctionCalendarProvider(Settings()).get_events()
    assert events[0]['release_time']=='2026-11-25T16:30:00+00:00'
    assert events[1]['release_time'] is None
    payload={'context':{'upcoming_events':[events[1]]}}
    assert upcoming_risk(payload,datetime(2026,11,25,15,tzinfo=timezone.utc))
    assert not upcoming_risk(payload,datetime(2026,11,24,15,tzinfo=timezone.utc))


@pytest.mark.parametrize('rows',[{},[],[dict(competitiveAuctionCloseDate='2026-11-25',competitiveAuctionCloseTime='bad')]])
def test_treasury_bad_response_never_becomes_complete_coverage(monkeypatch,rows):
    monkeypatch.setattr(cal.httpx,'get',lambda *a,**k:response(rows))
    with pytest.raises(RuntimeError):cal.TreasuryAuctionCalendarProvider(Settings()).get_events()


def test_fred_paginates_and_converts_eastern_daylight_time(monkeypatch):
    calls=[]
    def get(url,params,**kwargs):
        calls.append(params['offset'])
        if not params['offset']:
            return response({'count':1001,'release_dates':[{'release_name':'irrelevant','date':'2026-07-01'}]*1000})
        return response({'count':1001,'release_dates':[{'release_name':'Consumer Price Index','date':'2026-07-14'}]})
    monkeypatch.setattr(cal.httpx,'get',get)
    events=cal.FREDEconomicCalendarProvider(Settings(fred_api_key='test')).get_events()
    assert calls==[0,1000]
    assert events[0]['release_time']=='2026-07-14T12:30:00+00:00'


def test_composite_exposes_failed_feed_without_weakening_status():
    p=cal.CompositeOfficialCalendarProvider(Settings())
    def fail():raise RuntimeError('feed unavailable')
    p._providers=[SimpleNamespace(source='treasury',get_events=fail),SimpleNamespace(source='fed',get_events=lambda:[{'event':'Fed speech','release_time':'2026-11-30T18:00:00+00:00'}])]
    p.get_events()
    assert p.status=='PARTIAL' and any(g['source']=='treasury' for g in p.coverage_gaps)


def test_date_only_events_on_different_days_are_not_collapsed():
    assert len(cal._dedupe_events([{'event':'Auction','release_date':'2026-11-25'},
                                  {'event':'Auction','release_date':'2026-11-30'}]))==2


def test_general_event_gate_respects_published_date_risk_window():
    from app.services.trade_decision_card import _blocking_event
    now=datetime.now(timezone.utc)
    event={'event':'Auction time unannounced','importance':'high','release_time':None,
        'risk_window_start':(now-timedelta(hours=1)).isoformat(),
        'risk_window_end':(now+timedelta(hours=1)).isoformat()}
    assert _blocking_event({'context':{'upcoming_events':[event]}})
    event.update(risk_window_start=(now+timedelta(days=3)).isoformat(),
                 risk_window_end=(now+timedelta(days=4)).isoformat())
    assert not _blocking_event({'context':{'upcoming_events':[event]}})
