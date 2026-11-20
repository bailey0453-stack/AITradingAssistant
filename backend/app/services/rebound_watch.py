"""Always-on FIX worker research monitor. No dashboard request drives detection."""
import logging
import os
from datetime import datetime, timezone
from threading import Event, Lock, Thread
from time import monotonic

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.config import get_settings
from app.database import SessionLocal
from app.models.rebound import ReboundEvent
from app.services.dip_rebound import Detector, utc
from app.services.fix.quote_store import FixQuoteStore

logger = logging.getLogger(__name__)
_lock, _stop = Lock(), Event()
_detector, _thread = Detector(), None
_error = None


def persist(db, events):
    inserted = []
    for event in events:
        try:
            with db.begin_nested():
                db.add(ReboundEvent(event_key=event['key'], stage=event['stage'],
                                    at=utc(event['at']), payload=event))
                db.flush()
            inserted.append(event)
        except IntegrityError:
            pass
    db.commit()
    return inserted


def message(event):
    lines = ['FX Intelligence · PAPER RESEARCH ONLY',
             f"USD/MXN rebound: {event['stage']}", f"Quote time: {event['at']}"]
    if event['stage']=='ENTRY':
        lines += ['BUY USD / SELL MXN · setup observed',
                  f"Entry ask {event['entry_ask']:.5f}; target BID {event['target_bid']:.5f}; stop BID {event['stop_bid']:.5f}",
                  f"$100K net at target ${event['target_net_usd']:.2f}; net at stop ${event['stop_net_usd']:.2f}; includes $40 fees.",
                  f"Review deadline: {event['deadline']}"]
    elif 'realized_paper_net_usd' in event:
        lines.append(f"Observed paper net: ${event['realized_paper_net_usd']:.2f}")
    lines.append('Unvalidated rules; no probability established. No order sent. Do not use an old alert as a new entry.')
    return '\n'.join(lines)


def deliver(event):
    """Use local Telegram credentials or the authenticated web-app relay."""
    settings = get_settings()
    if settings.telegram_configured:
        from app.services.telegram_alerts import send_message
        return send_message(message(event))
    if settings.rebound_alert_relay_url and settings.rebound_alert_relay_secret:
        import httpx
        response = httpx.post(
            settings.rebound_alert_relay_url,
            headers={'X-Rebound-Secret': settings.rebound_alert_relay_secret},
            json=event, timeout=8.0, follow_redirects=False,
        )
        response.raise_for_status()
        return bool(response.json().get('delivered'))
    return False


def _run():
    global _error
    settings = get_settings()
    last_dip_alert = float('-inf')
    while not _stop.is_set():
        try:
            now = datetime.now(timezone.utc)
            q = FixQuoteStore.get().get_quote(settings.centroid_md_symbol_usdmxn)
            from app.services.market_hours import get_market_state
            fix = q.to_dict() if q and get_market_state(now=now).is_open else None
            with _lock:
                events = _detector.observe(fix, now)
            if events:
                with SessionLocal() as db:
                    events = persist(db, events)
                if settings.rebound_paper_alerts:
                    for event in events:
                        if event['stage']=='DIP':
                            if monotonic()-last_dip_alert < 300:
                                continue
                            last_dip_alert = monotonic()
                        try:
                            if not deliver(event):
                                logger.warning('Rebound paper alert not delivered')
                        except Exception:
                            # Delivery failure must not invalidate a valid paper plan.
                            logger.warning('Rebound paper alert delivery failed')
            _error = None
        except Exception:
            logger.exception('Rebound paper monitor failed')
            _error = 'Paper monitor error; check worker logs.'
            # Invalidate rather than silently continue through a storage outage.
            with _lock:
                _detector._gap(datetime.now(timezone.utc), _error)
        _stop.wait(1)


def start():
    global _thread
    settings = get_settings()
    if (os.getenv('VERCEL') or not settings.rebound_paper_enabled or settings.use_mock_data
            or not settings.centroid_md_configured or not settings.centroid_md_enabled):
        return
    if _thread and _thread.is_alive():
        return
    _stop.clear()
    _thread = Thread(target=_run, name='rebound-paper-watch', daemon=True)
    _thread.start()


def stop():
    _stop.set()
    if _thread:
        _thread.join(timeout=10)


def local_status(db):
    with _lock:
        out = _detector.snapshot(datetime.now(timezone.utc))
    running = bool(_thread and _thread.is_alive())
    if not running:
        out.update(status='DISABLED', reason='Paper monitor is not running on this worker.', setup=None)
    if _error:
        out.update(status='ERROR', reason=_error, setup=None)
    rows = db.execute(select(ReboundEvent).order_by(ReboundEvent.at.desc()).limit(30)).scalars().all()
    return {**out, 'running':running, 'poll_seconds':1,
            'alerts_configured':get_settings().rebound_paper_alerts and bool(
                get_settings().telegram_configured or
                (get_settings().rebound_alert_relay_url and get_settings().rebound_alert_relay_secret)),
            'recent_events':[row.payload for row in rows],
            'delivery_note':'Best-effort Telegram after persistence; failed delivery is not retried. Events remain available here.'}


def current(db):
    if os.getenv('VERCEL'):
        from app.services.fix.provider import _remote_worker_request
        try:
            result = _remote_worker_request(get_settings(), 'GET', '/admin/research/snapshots/rebound-status')
            if result:
                quote_at = result.get('quote_at')
                if not quote_at or not 0 <= (datetime.now(timezone.utc)-utc(quote_at)).total_seconds() <= 10:
                    result.update(status='DATA GAP',reason='Worker quote unavailable or stale.',setup=None)
                return result
        except RuntimeError:
            pass
        return {'status':'UNAVAILABLE','reason':'Persistent worker unavailable.','setup':None,'mode':'paper','validated':False,'live_execution':False}
    return local_status(db)
