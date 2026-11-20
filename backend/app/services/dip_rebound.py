"""Causal USD/MXN rebound research detector. Paper signals only; no orders.

Uses completed bid candles for structure and the current ask for entry. Missing
quotes reset the pattern: no invented candles or mid-price fills are allowed.
"""
from collections import deque
from datetime import datetime, timedelta, timezone
from math import isfinite


def utc(value):
    if not isinstance(value, datetime):
        value = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


def net(entry, exit_rate):
    return round(100_000 * (1 - entry / exit_rate) - 40, 2)


class Detector:
    def __init__(self):
        self.bars = deque(maxlen=61)
        self.bar = None
        self.last_at = None
        self.plan = None
        self.used = set()
        self.status = 'WARMING UP'
        self.reason = 'Collecting at least 20 uninterrupted completed one-minute broker candles.'

    def observe(self, fix, now):
        now = utc(now)
        try:
            bid, ask, at = float(fix['bid']), float(fix['ask']), utc(fix['updated_at'])
            valid = (all(isfinite(x) and x > 0 for x in (bid, ask))
                     and ask >= bid and -2 <= (now-at).total_seconds() <= 10
                     and 'USDMXN' in str(fix.get('symbol', '')).upper().replace('/', '')
                     and fix.get('source') == 'centroid_fix')
        except (ValueError, TypeError, KeyError, OverflowError):
            valid = False
        if not valid:
            return self._gap(now, 'Fresh broker bid/ask unavailable; no entry signal.')
        if self.last_at and at <= self.last_at:
            return []
        events = []
        if self.last_at and (at-self.last_at).total_seconds() > 10:
            events += self._gap(now, 'Quote coverage gap; restarting pattern collection.')
        self.last_at = at
        if self.plan:
            p = self.plan
            # Quote sampling may overshoot a stop: use the observed exit bid.
            result = ('STOP' if bid <= p['stop_bid'] else
                      'TARGET' if bid >= p['target_bid'] else
                      'TIMEOUT' if at >= utc(p['deadline']) else None)
            if result:
                events.append(self._event(result, at, {**p, 'exit_bid': bid,
                                                       'realized_paper_net_usd': net(p['entry_ask'], bid)}))
                self.plan = None
                self.status, self.reason = result, 'Paper outcome observed; no broker trade was opened or closed.'
        minute = at.replace(second=0, microsecond=0)
        if self.bar and minute != self.bar['at']:
            self.bars.append(self.bar)
            self.bar = None
            if not self.plan:
                events += self._evaluate(bid, ask, at)
        if self.bar is None:
            self.bar = dict(at=minute, high=bid, low=bid, close=bid)
        else:
            self.bar.update(high=max(self.bar['high'], bid), low=min(self.bar['low'], bid), close=bid)
        return events

    def _gap(self, now, reason):
        events = []
        if self.plan:
            events.append(self._event('COVERAGE_GAP', now, self.plan))
        self.bars.clear()
        self.bar = self.last_at = self.plan = None
        self.status, self.reason = 'DATA GAP', reason
        return events

    def _event(self, stage, at, details):
        origin = details.get('dip_at', at.isoformat())
        return dict(key=f'{origin}:{stage}', stage=stage, at=at.isoformat(), **details)

    def _evaluate(self, bid, ask, at):
        bars = list(self.bars)
        if len(bars) < 20:
            return []
        peak = max(range(len(bars)), key=lambda i: bars[i]['high'])
        tail = bars[peak+1:]
        if not tail:
            self.status, self.reason = 'SCANNING', 'No eight-cent dip from an earlier high.'
            return []
        trough = peak+1+min(range(len(tail)), key=lambda i: tail[i]['low'])
        top, low = bars[peak]['high'], bars[trough]['low']
        if top-low < .08-1e-9:
            self.status, self.reason = 'SCANNING', 'No eight-cent dip from an earlier high.'
            return []
        dip_at = bars[trough]['at'].isoformat()
        # Bound duplicate keys with the rolling observation window.
        self.used = {key for key in self.used if key >= bars[0]['at'].isoformat()}
        events = []
        if dip_at+':DIP' not in self.used:
            self.used.add(dip_at+':DIP')
            events.append(self._event('DIP', at, dict(dip_at=dip_at, dip_low=low, prior_high=top)))
        self.status, self.reason = 'DIP FORMING', 'Need a confirmed higher low and break of its preceding swing high.'
        if dip_at+':ENTRY' in self.used:
            return events
        # A local higher low is confirmed by candles on both sides. The final
        # completed candle must break the high formed before that pullback.
        for i in range(len(bars)-2, trough+1, -1):
            pullback = bars[i]
            if not (pullback['low'] > low+.002 and pullback['low'] < bars[i-1]['low']
                    and pullback['low'] < bars[i+1]['low']):
                continue
            swing = max(b['high'] for b in bars[trough+1:i])
            if bars[-1]['close'] <= swing+.001 or bid <= swing+.001:
                continue
            stop = pullback['low']-.003
            target = min(top, ask+.06)
            reward, risk = net(ask, target), -net(ask, stop)
            if (ask-bid > .012 or target-ask < .05-1e-9 or stop >= bid
                    or risk <= 0 or reward/risk < 1.5):
                self.status, self.reason = 'SKIP COST / RISK', 'Spread, remaining reward or net reward/risk fails the entry filter.'
                return events
            self.plan = dict(dip_at=dip_at, dip_low=low, prior_high=top,
                             entry_ask=round(ask,6), entry_bid=round(bid,6),
                             target_bid=round(target,6), stop_bid=round(stop,6),
                             target_net_usd=reward, stop_net_usd=-risk,
                             net_reward_risk=round(reward/risk,3), fees_usd=40,
                             deadline=(at+timedelta(hours=5)).isoformat())
            self.used.add(dip_at+':ENTRY')
            self.status, self.reason = 'PAPER BUY SETUP', 'Higher low and swing-high break observed; unvalidated research setup.'
            events.append(self._event('ENTRY', at, self.plan))
            return events
        return events

    def snapshot(self, now):
        fresh = self.last_at and 0 <= (utc(now)-self.last_at).total_seconds() <= 10
        return dict(status=self.status if fresh else 'DATA GAP',
                    reason=self.reason if fresh else 'No fresh broker quote; entry guidance unavailable.',
                    quote_at=self.last_at.isoformat() if self.last_at else None,
                    completed_minutes=len(self.bars), setup=dict(self.plan) if self.plan and fresh else None,
                    mode='paper', live_execution=False, validated=False,
                    confidence=None, note='Research rules, not a calibrated forecast. Polling can miss between-quote touches; $100K, $40 fees. No order is placed.')


def replay(quotes):
    """Replay ordered executable quotes without lookahead using their timestamps.

    Input must contain both bid/ask and genuine broker provenance. Unfinished
    plans and coverage gaps are reported separately, never counted as wins.
    """
    detector, events = Detector(), []
    for quote in quotes:
        events.extend(detector.observe(quote, utc(quote['updated_at'])))
    outcomes = [e for e in events if e['stage'] in {'TARGET','STOP','TIMEOUT'}]
    return dict(events=events, completed=len(outcomes),
                net_usd=round(sum(e['realized_paper_net_usd'] for e in outcomes),2),
                coverage_gaps=sum(e['stage']=='COVERAGE_GAP' for e in events),
                unfinished=detector.plan is not None, validated=False)
