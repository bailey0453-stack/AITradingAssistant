"""Owner isolation, idempotency, immutable levels and explicit user-reported exits."""
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.config import get_settings
from app.database import get_db
from app.main import app
from app.models import MarketSnapshot, ObservedFixQuote, PersonalTrade
from app.routers import personal_trades as routes
from app.services import customer_auth, personal_exit_watch
from app.services.fx_observations import capture, points


@pytest.fixture
def api(monkeypatch):
    monkeypatch.setenv("USE_MOCK_DATA","true")
    monkeypatch.setenv("AITA_REQUIRE_CUSTOMER_AUTH","true")
    monkeypatch.setenv("ADMIN_SECRET","test-personal-admin")
    get_settings.cache_clear()
    engine=create_engine("sqlite://",connect_args={"check_same_thread":False},poolclass=StaticPool)
    for model in (MarketSnapshot,ObservedFixQuote,PersonalTrade): model.__table__.create(engine)
    def db_override():
        with Session(engine) as db: yield db
    app.dependency_overrides[get_db]=db_override
    monkeypatch.setattr(customer_auth,"get_customer_session",lambda request:{"userId":request.headers["x-test-user"]} if request.headers.get("x-test-user") else None)
    def context(db):
        now=datetime.now(timezone.utc)
        return {"market_state":{"is_open":True},"context":{"upcoming_events":[]},
                "news_watch":{"status":"LIVE_POLL","checked_at":now.isoformat(),"items":[]}}
    monkeypatch.setattr(routes,"context",context)
    monkeypatch.setattr("app.services.fix.provider.get_fix_quote",lambda:{"bid":18.51,"ask":18.5102,"updated_at":datetime.now(timezone.utc).isoformat()})
    client=TestClient(app)
    try: yield client,engine
    finally:
        client.close();app.dependency_overrides.pop(get_db,None);engine.dispose();get_settings.cache_clear()


def entry(client,user="alice",request_id=None,direction="BUY_USD",rate=18.5):
    return client.post("/decision/positions",headers={"x-test-user":user},json={
        "request_id":request_id or str(uuid4()),"direction":direction,"entry_rate":rate})


def test_positions_require_a_named_customer_even_with_admin_secret(api):
    client,_=api
    assert client.get("/decision/positions").status_code==401
    assert client.get("/decision/positions",headers={"Authorization":"Bearer test-personal-admin"}).status_code==401


def test_one_open_entry_and_exact_retry_do_not_create_duplicate_trades(api):
    client,engine=api
    token=str(uuid4())
    first=entry(client,request_id=token)
    assert first.status_code==200,first.text
    assert first.json()["position"]["target_rate"]==18.55
    assert entry(client,request_id=token).status_code==200
    assert entry(client).status_code==409
    with Session(engine) as db: assert len(db.execute(select(PersonalTrade)).scalars().all())==1


def test_another_user_cannot_read_or_close_your_position(api):
    client,_=api
    first=entry(client).json()["position"]
    assert client.get("/decision/positions",headers={"x-test-user":"bob"}).json()["position"] is None
    response=client.post(f"/decision/positions/{first['id']}/close",headers={"x-test-user":"bob"},json={"exit_rate":18.55})
    assert response.status_code==404
    own=client.get("/decision/positions",headers={"x-test-user":"alice"}).json()["position"]
    assert own["id"]==first["id"] and "owner_key" not in own


def test_levels_survive_new_quotes_and_only_explicit_exit_releases_slot(api):
    client,engine=api
    p=entry(client).json()["position"]
    for _ in range(2):
        current=client.get("/decision/positions",headers={"x-test-user":"alice"}).json()["position"]
        assert [current[k] for k in ("entry_rate","target_rate","stop_rate","opened_at","review_at")]==[p[k] for k in ("entry_rate","target_rate","stop_rate","opened_at","review_at")]
    url=f"/decision/positions/{p['id']}/close"
    closed=client.post(url,headers={"x-test-user":"alice"},json={"exit_rate":18.55})
    assert closed.status_code==200 and closed.json()["position"]["action"]=="CLOSED BY YOU"
    retry=client.post(url,headers={"x-test-user":"alice"},json={"exit_rate":18.60})
    assert retry.json()["position"]["current_exit_rate"]==18.55
    assert entry(client).status_code==200
    with Session(engine) as db:
        row=db.get(PersonalTrade,p["id"])
        assert row.exit_rate==18.55 and row.owner_slot is None


@pytest.mark.parametrize("rate",[0,-1,1000])
def test_invalid_fill_rates_cannot_be_recorded(api,rate):
    client,_=api
    assert entry(client,rate=rate).status_code==422


def test_nonfinite_rates_and_caller_supplied_owner_are_rejected(api):
    client,_=api
    body='{"request_id":"'+str(uuid4())+'","direction":"BUY_USD","entry_rate":NaN}'
    assert client.post("/decision/positions",headers={"x-test-user":"alice","Content-Type":"application/json"},content=body).status_code==422
    assert client.post("/decision/positions",headers={"x-test-user":"alice"},json={"request_id":str(uuid4()),"direction":"BUY_USD","entry_rate":18.5,"owner_key":"bob"}).status_code==422


def test_production_refuses_ephemeral_storage(api,monkeypatch):
    client,_=api
    monkeypatch.setattr(routes,"get_settings",lambda:SimpleNamespace(is_mock=False))
    assert entry(client).status_code==503
    assert client.get("/decision/positions",headers={"x-test-user":"alice"}).status_code==503


def test_only_genuine_fresh_quotes_are_saved_once_per_minute(api):
    _,engine=api
    now=datetime.now(timezone.utc)
    quote={"symbol":"USDMXN.r","source":"centroid_fix","bid":18.5,"ask":18.5002,"updated_at":now.isoformat()}
    with Session(engine) as db:
        capture(db,quote,now);capture(db,quote,now)
        capture(db,dict(quote,updated_at=(now-timedelta(minutes=2)).isoformat()),now)
        db.commit()
        assert len(db.execute(select(ObservedFixQuote)).scalars().all())==1
        for provider,source in [("mock","mock"),("openexchangerates","live"),("centroid_fix","live")]:
            db.add(MarketSnapshot(created_at=now-timedelta(minutes=5),pair="USDMXN",usdmxn=18.49,provider=provider,source=source))
        db.commit()
        assert len(points(db,now,recent_only=True))==2


def test_background_retains_exit_advice_while_page_is_closed(api):
    client,engine=api
    p=entry(client).json()["position"]
    now=datetime.now(timezone.utc)+timedelta(seconds=1)
    quote={"bid":18.55,"ask":18.5502,"updated_at":now.isoformat()}
    with Session(engine) as db:
        result=personal_exit_watch.background(db,quote,{"market_state":{"is_open":True}},now=now)
        assert result["positions_checked"]==1
    with Session(engine) as db:
        row=db.get(PersonalTrade,p["id"])
        assert row.exit_advice=="TAKE PROFIT" and row.closed_at is None
