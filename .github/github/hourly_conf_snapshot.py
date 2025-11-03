#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
hourly_conf_snapshot.py
Compute current confidence (heat) per coin using the most recent CLOSED 1h kline
as the "current price", but keep the 20-day daily-return model unchanged.

Output matches your scale (0-100), with trigger deltas to 77/23.
"""

import requests, time
from datetime import datetime, timezone, timedelta

COINS = [("BTCUSDT","BTC"), ("ETHUSDT","ETH"), ("SOLUSDT","SOL")]
CONF_TRIGGER = 77

BASES = [
    "https://api.binance.com",
    "https://api1.binance.com",
    "https://api2.binance.com",
    "https://api3.binance.com",
    "https://data-api.binance.vision",
]
HEADERS = {"User-Agent":"hourly-conf-snapshot/1.0 (+bbot)"}

def binance_klines(symbol, interval, limit, end_time_ms=None):
    last_err=None
    for base in BASES:
        try:
            url=f"{base}/api/v3/klines"
            params={"symbol":symbol,"interval":interval,"limit":limit}
            if end_time_ms is not None:
                params["endTime"]=end_time_ms
            r=requests.get(url,params=params,headers=HEADERS,timeout=20)
            r.raise_for_status()
            return r.json()
        except Exception as e:
            last_err=e
            continue
    raise last_err if last_err else RuntimeError("All Binance bases failed")

def fully_closed_daily(symbol, limit=1500):
    # end at today's 00:00 UTC - 1ms so we only get finished daily candles
    now=datetime.utcnow().replace(tzinfo=timezone.utc)
    midnight=datetime(now.year, now.month, now.day, tzinfo=timezone.utc)
    end_ms=int(midnight.timestamp()*1000) - 1
    ks = binance_klines(symbol, "1d", limit, end_ms)
    rows=[]
    for k in ks:
        close_ts=int(k[6])//1000
        close=float(k[4])
        rows.append((datetime.utcfromtimestamp(close_ts).date(), close))
    return rows

def last_closed_hour(symbol):
    # end at current hour start - 1ms
    now=datetime.utcnow().replace(minute=0, second=0, microsecond=0, tzinfo=timezone.utc)
    end_ms=int(now.timestamp()*1000) - 1
    ks=binance_klines(symbol,"1h",2,end_ms)  # need only last closed
    k=ks[-1]
    close_ts=int(k[6])//1000
    close=float(k[4])
    return datetime.utcfromtimestamp(close_ts), close

def pct_returns(closes):
    return [closes[i]/closes[i-1]-1 for i in range(1,len(closes))]

def zscore(value, window):
    mu = sum(window)/len(window)
    var = sum((x-mu)**2 for x in window)/len(window)
    sd = var**0.5
    if sd<=0: return None
    return abs((value - mu)/sd)

def heat_from_return(current_ret, window_returns):
    z = zscore(current_ret, window_returns)
    if z is None: return None
    z_signed = z if current_ret>0 else -z
    level = round(max(0, min(100, 50 + z_signed*20)))
    return level

def fmtp(x):
    if x>=1000: return f"{x:,.2f}"
    if x>=1:    return f"{x:,.2f}"
    return f"{x:,.4f}"

def main():
    print("📍 Hourly snapshot vs daily model (20-day lookback)")
    print(f"UTC now: {datetime.utcnow().strftime('%Y-%m-%d %H:%M')}\n")

    for symbol, sym in COINS:
        try:
            daily = fully_closed_daily(symbol)
            dates, closes = zip(*daily)
            if len(closes) < 22:
                print(f"{sym}: not enough daily data")
                continue

            # 20-day daily-return window: use the 20 returns BEFORE today
            daily_rets = pct_returns(list(closes))
            window = daily_rets[-20-1:-1]  # last 20 fully past returns

            # anchor = yesterday close
            prev_daily_close = closes[-1]

            # current = last CLOSED hourly close
            hour_dt, hour_px = last_closed_hour(symbol)

            # map "current return" against daily distribution
            cur_ret = hour_px/prev_daily_close - 1.0

            level = heat_from_return(cur_ret, window)
            if level is None:
                print(f"{sym}: sd=0 window, cannot compute")
                continue

            if level >= CONF_TRIGGER:
                mood = "🔴 Overbought"
                gap = 0
            elif level <= 100 - CONF_TRIGGER:
                mood = "🟢 Oversold"
                gap = 0
            else:
                mood = "⚪ Neutral"
                # distance to nearest trigger (up to short or down to long)
                gap_up   = CONF_TRIGGER - level       # to short threshold
                gap_down = level - (100 - CONF_TRIGGER)  # to long threshold
                gap = min(gap_up, gap_down)

            line = (f"{mood} {sym}: {level}%  "
                    f"(hour close {hour_dt.strftime('%H:%M')} UTC = ${fmtp(hour_px)}; "
                    f"prev daily close ${fmtp(prev_daily_close)})")
            if gap:
                line += f"  · {gap:.0f}% to trigger"
            print(line)

            time.sleep(0.12)
        except Exception as e:
            print(f"{sym}: ⚠️ {e}")

if __name__ == "__main__":
    main()
