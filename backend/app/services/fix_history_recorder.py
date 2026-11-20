"""Durable minute observations from the local FIX feed, independent of browsers."""
import logging
import os
from datetime import datetime, timezone
from threading import Event, Thread

from sqlalchemy import func, select

from app.config import get_settings
from app.database import SessionLocal, database_is_persistent
from app.models import ObservedFixQuote
from app.services.fx_observations import capture, usable_quote
from app.services.fix.quote_store import FixQuoteStore

logger = logging.getLogger(__name__)
_stop = Event()
_thread = None
_last_minute = None
_error = None


def sample(fix, now):
    global _last_minute, _error
    quote = usable_quote(fix, now)
    if not quote or not isinstance(fix,dict) or fix.get('source')!='centroid_fix' or str(fix.get('symbol','')).upper().replace('/','').split('.')[0]!='USDMXN':
        return False
    minute = quote[2].replace(second=0,microsecond=0)
    if _last_minute is not None and minute <= _last_minute:
        return False
    # Set the in-memory cursor only after a successful commit; retry DB failures.
    with SessionLocal() as db:
        inserted = capture(db,fix,now)
        db.commit()
    _last_minute, _error = minute, None
    return inserted


def _run():
    global _error
    while not _stop.is_set():
        try:
            from app.services.market_hours import get_market_state
            now=datetime.now(timezone.utc)
            if get_market_state(now=now).is_open:
                q=FixQuoteStore.get().get_quote(get_settings().centroid_md_symbol_usdmxn)
                sample(q.to_dict() if q else None,now)
        except Exception:
            _error='Minute history could not be saved; retrying. Check worker logs.'
            logger.warning('FIX minute-history persistence failed',exc_info=False)
        _stop.wait(1)


def start():
    global _thread
    s=get_settings()
    if os.getenv('VERCEL') or s.is_mock or not s.centroid_md_enabled or not s.centroid_md_configured:
        return
    if not database_is_persistent():
        logger.error('FIX minute history requires persistent Postgres storage')
        return
    if _thread and _thread.is_alive():
        return
    _stop.clear()
    _thread=Thread(target=_run,name='fix-minute-history',daemon=True)
    _thread.start()


def stop():
    _stop.set()
    if _thread:
        _thread.join(timeout=10)


def status(db):
    count,first,last=db.execute(select(func.count(ObservedFixQuote.id),
        func.min(ObservedFixQuote.quote_at),func.max(ObservedFixQuote.quote_at))).one()
    return {'running':bool(_thread and _thread.is_alive()),'persistent':database_is_persistent(),
            'observations':count,'first_quote_at':first.isoformat() if first else None,
            'last_quote_at':last.isoformat() if last else None,'error':_error,
            'sampling':'One fresh executable broker observation per minute; missing minutes are never filled.'}
