from datetime import datetime, timezone
from app.config import Settings
from app.services import macro_data, market_data
from app.services.signal_weights import score_signals

def _settings(): return Settings(use_mock_data=False,fred_api_key="x"*32,alpha_vantage_api_key="y"*16)
def _live_fix(monkeypatch,rate=17.0): monkeypatch.setattr(market_data,"_fresh_fix_midpoint",lambda *a,**k:(rate,datetime.now(timezone.utc).isoformat()))
def _meta(monkeypatch, value): monkeypatch.setattr(macro_data,"get_macro_field_meta",lambda:value)

def test_unavailable_macro_is_never_replaced_with_mock_values(monkeypatch):
    _live_fix(monkeypatch); monkeypatch.setattr(macro_data,"fetch_live_macro",lambda settings:{}); _meta(monkeypatch,{})
    data=market_data.get_market_data(_settings())
    assert data.usdmxn==17.0 and data.field_sources["usdmxn"]=="live"
    for field in market_data.MACRO_FIELDS:
        assert getattr(data,field) is None; assert data.field_sources[field]=="unavailable"
    for key in ("dxy_delta","yield_delta","us2y_delta","oil_delta","gold_delta","sp_delta","vix_delta"): assert data.drivers[key]==0.0
    scored=score_signals(data,news=[],released_events=[],momentum=None)
    assert scored["weighted_contributions"]==[] and scored["net_score"]==0.0 and scored["direction"]=="HOLD"

def test_partial_live_macro_keeps_only_verified_fields(monkeypatch):
    _live_fix(monkeypatch,16.95)
    monkeypatch.setattr(macro_data,"fetch_live_macro",lambda settings:{"us2y":4.71,"us10y":4.29,"treasury_yield":4.29,"oil":77.2})
    _meta(monkeypatch,{f:{"status":"live","provider":"fred" if f.startswith("us") else "yahoo","age_seconds":0} for f in ("us2y","us10y","oil")})
    data=market_data.get_market_data(_settings())
    assert (data.us2y,data.us10y,data.treasury_yield,data.oil)==(4.71,4.29,4.29,77.2)
    for f in ("us2y","us10y","oil"): assert data.field_sources[f]=="live"
    for f in ("dxy","gold","sp_futures","vix"): assert getattr(data,f) is None and data.field_sources[f]=="unavailable"

def test_verified_stale_cache_is_explicit_not_live(monkeypatch):
    _live_fix(monkeypatch)
    monkeypatch.setattr(macro_data,"fetch_live_macro",lambda settings:{"dxy":101.25})
    _meta(monkeypatch,{"dxy":{"status":"stale_cache","provider":"yahoo","age_seconds":1800}})
    data=market_data.get_market_data(_settings())
    assert data.dxy==101.25
    assert data.field_sources["dxy"]=="stale_cache"
    assert data.field_meta["dxy"]["provider"]=="yahoo"
    assert data.field_meta["dxy"]["age_seconds"]==1800
