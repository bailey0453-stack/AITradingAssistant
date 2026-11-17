"""Authenticated owner-scoped records of entries/exits the user reports."""
import hashlib
from datetime import datetime, timedelta, timezone
from typing import Literal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.routing import APIRoute
from pydantic import BaseModel, Field, ConfigDict
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.database import get_db
from app.config import get_settings
from app.models import PersonalTrade
from app.services.customer_auth import require_customer_session
from app.services import personal_exit_watch as watch

class PersonalTradeRoute(APIRoute):
    def get_route_handler(self):
        handler=super().get_route_handler()
        async def safe_handler(request):
            try:
                return await handler(request)
            except RequestValidationError as exc:
                # Do not echo personal input or non-JSON NaN/Infinity values.
                errors=[{k:e[k] for k in ("loc","msg","type")} for e in exc.errors()]
                return JSONResponse(status_code=422,content={"detail":errors})
        return safe_handler


router = APIRouter(prefix="/decision/positions",tags=["personal trades"],route_class=PersonalTradeRoute)


def owner(session=Depends(require_customer_session)):
    uid = session.get("userId")
    if uid is None or not str(uid).strip():
        raise HTTPException(403,"A named personal session is required")
    return hashlib.sha256(("user:"+str(uid)).encode()).hexdigest()


class Entry(BaseModel):
    model_config = ConfigDict(extra="forbid",allow_inf_nan=False)
    request_id: UUID
    direction: Literal["BUY_USD","SELL_USD"]
    entry_rate: float = Field(gt=0,lt=1000,strict=True)


class Exit(BaseModel):
    model_config = ConfigDict(extra="forbid",allow_inf_nan=False)
    exit_rate: float = Field(gt=0,lt=1000,strict=True)


def context(db):
    from app.routers.decision import _short_term_calendar
    from app.services.market_hours import get_market_state
    from app.services.short_term_news import snapshot
    from time import time
    return {**_short_term_calendar(int(time()//300)),"market_state":{"is_open":get_market_state().is_open},"news_watch":snapshot(db)}


def require_storage(db):
    if db.get_bind().dialect.name != "postgresql" and not get_settings().is_mock:
        raise HTTPException(503,"Persistent trade storage is unavailable; no entry or exit was recorded")


@router.get("")
def position(owner_key=Depends(owner),db: Session=Depends(get_db)):
    require_storage(db)
    from app.services.fix.provider import get_fix_quote
    payload = context(db)
    return watch.active(db,owner_key,get_fix_quote(),payload)


@router.post("")
def record_entry(body: Entry,owner_key=Depends(owner),db: Session=Depends(get_db)):
    require_storage(db)
    now = datetime.now(timezone.utc)
    existing = db.get(PersonalTrade,str(body.request_id))
    if existing:
        if existing.owner_key != owner_key:
            raise HTTPException(409,"Entry identifier is already in use")
        return {"position":watch.assess(existing,None,[],{},now=now),"recorded":True}
    if db.execute(select(PersonalTrade.id).where(PersonalTrade.owner_slot==owner_key)).first():
        raise HTTPException(409,"Record the exit of your existing trade before adding another")
    sign = 1 if body.direction=="BUY_USD" else -1
    stop = round(body.entry_rate-sign*.05,6)
    target = round(body.entry_rate+sign*.05,6)
    if min(stop,target)<=0:
        raise HTTPException(422,"Entry rate cannot support these five-cent levels")
    trade = PersonalTrade(id=str(body.request_id),owner_key=owner_key,owner_slot=owner_key,
        direction=body.direction,entry_rate=body.entry_rate,target_rate=target,stop_rate=stop,
        opened_at=now,review_at=now+timedelta(hours=5),peak_rate=body.entry_rate)
    db.add(trade)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(409,"An entry was already recorded; refresh your position")
    return {"position":watch.assess(trade,None,[],{},now=now),"recorded":True}


@router.post("/{trade_id}/close")
def record_exit(trade_id: UUID,body: Exit,owner_key=Depends(owner),db: Session=Depends(get_db)):
    require_storage(db)
    trade = db.execute(select(PersonalTrade).where(PersonalTrade.id==str(trade_id),PersonalTrade.owner_key==owner_key)
                       .with_for_update()).scalar_one_or_none()
    if trade is None:
        raise HTTPException(404,"Trade not found")
    if not trade.closed_at:
        trade.closed_at = datetime.now(timezone.utc)
        trade.exit_rate = body.exit_rate
        trade.owner_slot = None
        db.commit()
    return {"position":watch.assess(trade,None,[],{}),"recorded":True}
