"""Persistent personal trade journal and rule-based exit guidance, never execution."""
from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from app.models import ObservedFixQuote, PersonalTrade
from app.services.flexible_opportunity import _near, net_pnl, upcoming_risk
from app.services.fx_observations import aware, capture, points, usable_quote
from app.services.short_term_news import _interpret


def assess(trade, fix, prices, payload, *, observations=(), now=None):
    now = aware(now or datetime.now(timezone.utc))
    sign = 1 if trade.direction == "BUY_USD" else -1
    out = {"id":trade.id,"direction":trade.direction,"entry_rate":trade.entry_rate,
           "target_rate":trade.target_rate,"stop_rate":trade.stop_rate,
           "opened_at":aware(trade.opened_at).isoformat(),"review_at":aware(trade.review_at).isoformat(),
           "action":"CHECK LIVE QUOTE","reason":"A fresh executable quote is required before assessing an exit.",
           "generated_at":now.isoformat(),"expires_at":(now+timedelta(seconds=40)).isoformat(),
           "current_exit_rate":None,"net_pnl_usd":None,"move_cents":None,"warnings":[],
           "basis":"Rule-based exit guidance, not a calibrated forecast. $100K reference and $40 estimated fees. No order is placed.",
           "monitoring":"Dashboard checks every 15 seconds; background checks about once a minute. Moves between observations may be missed."}
    if trade.closed_at:
        out.update(action="CLOSED BY YOU",reason="You recorded the exit; no broker order was submitted.",
                   closed_at=aware(trade.closed_at).isoformat(),current_exit_rate=trade.exit_rate,
                   net_pnl_usd=net_pnl(trade.direction,trade.entry_rate,trade.exit_rate))
        return out
    quote = usable_quote(fix,now)
    live = quote and quote[2] >= aware(trade.opened_at)
    seen = list(observations)
    if live:
        seen.append((quote[2],quote[0],quote[1]))
    seen = sorted({aware(t):(bid,ask) for t,bid,ask in seen
                   if aware(t) and aware(trade.opened_at) <= aware(t) <= now+timedelta(seconds=2)
                   and (trade.last_quote_at is None or aware(t)>aware(trade.last_quote_at))}.items())
    previous = aware(trade.last_quote_at or trade.opened_at)
    for at,(bid,ask) in seen:
        if at-previous > timedelta(minutes=2):
            trade.coverage_gap = True
        exit_rate = bid if sign == 1 else ask
        if sign*(exit_rate-trade.peak_rate)>0:
            trade.peak_rate = exit_rate
        if not trade.exit_advice:
            if sign*(exit_rate-trade.target_rate) >= -1e-9:
                trade.exit_advice = "TAKE PROFIT"
                trade.advice_reason = "Your original five-cent target was observed on the executable exit side."
                trade.advice_at = at
            elif sign*(exit_rate-trade.stop_rate) <= 1e-9:
                trade.exit_advice = "EXIT / STOP"
                trade.advice_reason = "Your original five-cent stop was observed on the executable exit side; do not widen it."
                trade.advice_at = at
        trade.last_quote_at = at
        previous = at
    if getattr(trade,"coverage_gap",False):
        out["warnings"].append("There was a gap between price observations; the first target/stop touch may have been missed.")
    out["peak_favorable_cents"] = round(max(0,sign*(trade.peak_rate-trade.entry_rate))*100,3)
    if trade.exit_advice:
        out.update(action=trade.exit_advice,reason=trade.advice_reason,
                   advice_at=aware(trade.advice_at).isoformat())
    if live:
        bid,ask,at = quote
        exit_rate = bid if sign == 1 else ask
        profit = sign*(exit_rate-trade.entry_rate)
        out.update(current_exit_rate=round(exit_rate,6),quote_at=at.isoformat(),
                   move_cents=round(profit*100,3),net_pnl_usd=net_pnl(trade.direction,trade.entry_rate,exit_rate))
    else:
        out["warnings"].append("Live quote unavailable or stale. Original target and stop still apply; check your trading platform.")
        return out
    if trade.exit_advice:
        out["warnings"].append("This observed exit advice remains until you record your actual exit. The app has not closed the trade.")
        return out
    if not (payload.get("market_state") or {}).get("is_open"):
        out.update(action="REASSESS",reason="The market is closed or its status is unverified; check executable prices with your broker.")
        return out
    mid = (bid+ask)/2
    times = [p[0] for p in prices]
    baselines = [_near(prices,times,now-timedelta(minutes=m)) for m in (5,15)]
    f = tuple(10_000*(mid/p[1]-1) for p in baselines) if all(p and p[0]<now for p in baselines) else None
    if sign*(trade.peak_rate-trade.entry_rate) >= .03-1e-9 and sign*(trade.peak_rate-exit_rate) >= .01-1e-9:
        if out["net_pnl_usd"] > 0:
            out.update(action="TAKE PROFIT EARLY",reason="The trade gained at least three cents, then gave back at least one cent. The profit-protection rule suggests exiting before the full target.")
        else:
            out.update(action="EXIT EARLY",reason="The trade gave back at least one cent after gaining three cents, and estimated net profit is no longer positive.")
    elif f and sign*f[0] < 0 and sign*f[1] < 0 and abs(f[0])*(bid+ask)/20_000 >= max(.01,ask-bid):
        out.update(action="EXIT EARLY",reason="Both five- and fifteen-minute price changes oppose your trade, with at least a one-cent reversal over five minutes.")
    elif now >= aware(trade.review_at):
        out.update(action="REASSESS",reason="Five hours have passed since entry. Review the original idea; the target and stop have not moved.")
    else:
        risks = upcoming_risk(payload,now)
        news = payload.get("news_watch") or {}
        news_checked = aware(news.get("checked_at"))
        news_ok = news.get("status")=="LIVE_POLL" and news_checked and -5 <= (now-news_checked).total_seconds() <= 90
        conflicting = False
        for item in news.get("items") or []:
            published = aware(item.get("published_at"))
            if published and timedelta(0) <= now-published <= timedelta(minutes=20):
                direction,shock = _interpret(item.get("headline"))
                if (direction and direction != trade.direction) or (shock and not direction):
                    conflicting = True
        if risks or conflicting:
            out.update(action="REASSESS",reason="An approaching report or fresh conflicting headline changes the risk. Check it before continuing to hold.")
        elif getattr(trade,"coverage_gap",False):
            out.update(action="REASSESS",reason="Price observations had a gap. Check your broker's price history before relying on the original trade idea.")
        elif not news_ok or f is None:
            out.update(action="REASSESS",reason="Price context or news coverage is incomplete; a supported HOLD assessment is unavailable.")
        elif sign*f[0] < 0:
            out.update(action="WATCH MOMENTUM",reason="The last five minutes oppose your trade, while the longer move has not fully reversed. Keep the original stop.")
        else:
            out.update(action="HOLD",reason="No target, stop, profit giveback or confirmed short-term reversal rule has triggered. Keep the original levels.")
    return out


def update(db, trade, fix, prices, payload, now):
    since = aware(trade.last_quote_at or trade.opened_at)
    observations = db.execute(select(ObservedFixQuote.quote_at,ObservedFixQuote.bid,ObservedFixQuote.ask)
        .where(ObservedFixQuote.quote_at>=since,ObservedFixQuote.quote_at<=now)
        .order_by(ObservedFixQuote.quote_at).limit(10_000)).all()
    return assess(trade,fix,prices,payload,observations=observations,now=now)


def active(db, owner, fix, payload, *, now=None):
    now = aware(now or datetime.now(timezone.utc))
    capture(db,fix,now)
    trade = db.execute(select(PersonalTrade).where(PersonalTrade.owner_slot==owner).with_for_update()).scalar_one_or_none()
    latest = db.execute(select(PersonalTrade).where(PersonalTrade.owner_key==owner,PersonalTrade.closed_at.is_not(None))
                        .order_by(PersonalTrade.closed_at.desc()).limit(1)).scalar_one_or_none()
    result = update(db,trade,fix,points(db,now,recent_only=True),payload,now) if trade else None
    closed = assess(latest,None,[],{},now=now) if latest else None
    db.commit()
    return {"position":result,"last_closed":closed}


def background(db, fix, payload, *, now=None):
    now = aware(now or datetime.now(timezone.utc))
    capture(db,fix,now)
    trades = db.execute(select(PersonalTrade).where(PersonalTrade.owner_slot.is_not(None))
                        .with_for_update().limit(100)).scalars().all()
    prices = points(db,now,recent_only=True) if trades else []
    for trade in trades:
        update(db,trade,fix,prices,payload,now)
    db.commit()
    return {"positions_checked":len(trades),"quote_live":usable_quote(fix,now) is not None}
