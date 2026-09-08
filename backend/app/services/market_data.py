"""Market data providers for USD/MXN and macro drivers.

Centroid/GFC FIX is the preferred USD/MXN source when a fresh executable
bid/ask is available. The neutral market spot is the FIX midpoint.

Production analysis never substitutes randomized mock values for unavailable
live inputs. Missing production fields are ``None``. Verified real macro cache
values may be reused with explicit ``cached`` / ``stale_cache`` source labels.
"""
from __future__ import annotations
import logging, random
from abc import ABC, abstractmethod
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
import httpx
from app.config import Settings, get_settings
logger=logging.getLogger(__name__)
def _utcnow_iso(): return datetime.now(timezone.utc).isoformat()
@dataclass
class MarketData:
    pair:str="USDMXN"; usdmxn:float|None=None; inverse_usdmxn:float|None=None
    dxy:float|None=None; us2y:float|None=None; us10y:float|None=None; treasury_yield:float|None=None
    oil:float|None=None; gold:float|None=None; sp_futures:float|None=None; vix:float|None=None
    provider:str="mock"; source:str="mock"; timestamp:str|None=None
    drivers:dict=field(default_factory=dict); field_sources:dict=field(default_factory=dict)
    field_meta:dict=field(default_factory=dict)
    def to_dict(self): return asdict(self)
MACRO_FIELDS=("dxy","us2y","us10y","oil","gold","sp_futures","vix")
class MarketDataProvider(ABC):
    source="base"
    @abstractmethod
    def get_usdmxn(self)->MarketData: raise NotImplementedError
def _inverse(v): return round(1.0/v,6) if v else None
class MockMarketDataProvider(MarketDataProvider):
    source=provider="mock"; BASE_USDMXN=17.85; BASE_DXY=104.2; BASE_US2Y=4.70; BASE_US10Y=4.32; BASE_OIL=76.5; BASE_GOLD=2380.; BASE_SP=5450.; BASE_VIX=14.5
    def _macro(self):
        y=round(self.BASE_US10Y+random.uniform(-.08,.08),3)
        return {"dxy":round(self.BASE_DXY+random.uniform(-.6,.6),2),"us2y":round(self.BASE_US2Y+random.uniform(-.07,.07),3),"us10y":y,"treasury_yield":y,"oil":round(self.BASE_OIL+random.uniform(-2.5,2.5),2),"gold":round(self.BASE_GOLD+random.uniform(-25,25),2),"sp_futures":round(self.BASE_SP+random.uniform(-40,40),2),"vix":round(self.BASE_VIX+random.uniform(-2.5,4),2)}
    def get_usdmxn(self): return self._assemble(round(self.BASE_USDMXN+random.uniform(-.25,.25),4),self._macro(),self.provider,self.source)
    @staticmethod
    def _delta(value,base,digits): return 0.0 if value is None else round(float(value)-base,digits)
    @classmethod
    def _assemble(cls,u,m,p,s): return MarketData(pair="USDMXN",usdmxn=u,inverse_usdmxn=_inverse(u),dxy=m["dxy"],us2y=m["us2y"],us10y=m["us10y"],treasury_yield=m["treasury_yield"],oil=m["oil"],gold=m["gold"],sp_futures=m["sp_futures"],vix=m["vix"],provider=p,source=s,timestamp=_utcnow_iso(),drivers=cls._drivers(u,m))
    @classmethod
    def _drivers(cls,u,m): return {"dxy_delta":cls._delta(m.get("dxy"),cls.BASE_DXY,3),"yield_delta":cls._delta(m.get("us10y"),cls.BASE_US10Y,3),"us2y_delta":cls._delta(m.get("us2y"),cls.BASE_US2Y,3),"oil_delta":cls._delta(m.get("oil"),cls.BASE_OIL,3),"gold_delta":cls._delta(m.get("gold"),cls.BASE_GOLD,2),"sp_delta":cls._delta(m.get("sp_futures"),cls.BASE_SP,2),"vix_delta":cls._delta(m.get("vix"),cls.BASE_VIX,2),"usdmxn_delta":cls._delta(u,cls.BASE_USDMXN,4)}
class LiveMarketDataProvider(MarketDataProvider):
    source="live"; DEFAULT_BASE_URL="https://openexchangerates.org/api/latest.json"
    def __init__(self,s): self.settings=s; self.base_url=s.fx_base_url or self.DEFAULT_BASE_URL; self.timeout=s.http_timeout_seconds
    def _fetch_usdmxn(self):
        if not self.settings.fx_api_key: raise RuntimeError("FX_API_KEY is not configured.")
        try:
            r=httpx.get(self.base_url,params={"symbols":"MXN"},headers={"Authorization":f"Token {self.settings.fx_api_key}"},timeout=self.timeout); r.raise_for_status(); d=r.json()
        except Exception as e: raise RuntimeError(f"FX request failed: {_scrub(str(e),self.settings.fx_api_key)}") from None
        rate=((d or {}).get("rates") or {}).get("MXN")
        if rate is None: raise ValueError("USD/MXN not present in FX response")
        return float(rate)
    def get_usdmxn(self): return MockMarketDataProvider._assemble(round(self._fetch_usdmxn(),4),MockMarketDataProvider()._macro(),self.settings.fx_provider or "live",self.source)
def _scrub(t,s): return t.replace(s,"***REDACTED***") if s and s in t else t
def get_market_provider(settings=None):
    s=settings or get_settings(); return LiveMarketDataProvider(s) if s.fx_live_enabled else MockMarketDataProvider()
def _mock_all(d): d.field_sources={f:"mock" for f in ("usdmxn",*MACRO_FIELDS)}; return d
def _fresh_fix_midpoint(max_age_seconds=30.):
    try:
        from app.services.fix.provider import get_fix_quote
        q=get_fix_quote()
        if not q or q.get("bid") is None or q.get("ask") is None or not q.get("updated_at"): return None
        dt=datetime.fromisoformat(str(q["updated_at"]).replace("Z","+00:00")); dt=dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
        if (datetime.now(timezone.utc)-dt).total_seconds()>max_age_seconds:return None
        return (float(q["bid"])+float(q["ask"]))/2,str(q["updated_at"])
    except Exception as e: logger.warning("Centroid FIX midpoint unavailable; using hourly FX fallback (%s).",e); return None
def get_market_data(settings=None):
    s=settings or get_settings()
    if s.is_mock:return _mock_all(MockMarketDataProvider().get_usdmxn())
    d=MarketData(provider="unavailable",source="fallback",timestamp=_utcnow_iso()); fs={}; fm={}
    fix=_fresh_fix_midpoint()
    if fix:
        spot,ts=fix; d.usdmxn=round(spot,5); d.inverse_usdmxn=_inverse(spot); d.provider="centroid_fix"; d.source="live"; d.timestamp=ts; fs["usdmxn"]="live"; fm["usdmxn"]={"status":"live","provider":"centroid_fix","age_seconds":0.0}
    elif s.fx_api_key:
        try:
            spot=round(LiveMarketDataProvider(s)._fetch_usdmxn(),4); d.usdmxn=spot; d.inverse_usdmxn=_inverse(spot); d.provider=s.fx_provider or "live"; d.source="live"; fs["usdmxn"]="live"; fm["usdmxn"]={"status":"live","provider":d.provider,"age_seconds":0.0}
        except Exception as e: logger.warning("Live FX fetch failed (%s); USD/MXN unavailable.",_scrub(str(e),s.fx_api_key)); fs["usdmxn"]="unavailable"; fm["usdmxn"]={"status":"unavailable","provider":None,"age_seconds":None}
    else: fs["usdmxn"]="unavailable"; fm["usdmxn"]={"status":"unavailable","provider":None,"age_seconds":None}
    lm={}; mm={}
    if s.macro_live_enabled:
        try:
            from app.services.macro_data import fetch_live_macro,get_macro_field_meta
            lm=fetch_live_macro(s); mm=get_macro_field_meta()
        except Exception as e: logger.warning("Macro fetch failed wholesale (%s); macro inputs unavailable.",_scrub(str(e),s.fred_api_key or ""))
    for f in MACRO_FIELDS:
        meta=mm.get(f,{"status":"unavailable","provider":None,"age_seconds":None}); fm[f]=meta
        if f in lm and lm[f] is not None:
            setattr(d,f,lm[f]); fs[f]=meta.get("status") or "live"
        else: setattr(d,f,None); fs[f]="unavailable"
    d.treasury_yield=lm.get("treasury_yield") if lm.get("treasury_yield") is not None else d.us10y
    m={"dxy":d.dxy,"us2y":d.us2y,"us10y":d.us10y,"treasury_yield":d.treasury_yield,"oil":d.oil,"gold":d.gold,"sp_futures":d.sp_futures,"vix":d.vix}
    d.drivers=MockMarketDataProvider._drivers(d.usdmxn,m); d.field_sources=fs; d.field_meta=fm
    if not fix:d.timestamp=_utcnow_iso()
    return d
