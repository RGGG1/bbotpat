#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Analyze MAE/MFE per trade using hourly klines, while generating daily 77% signals
for BTC/ETH/SOL from Jan 2023. One shared position at a time (BTC>ETH>SOL priority).
Exit on TP hit or 96h expiry (matching your live bot's core behavior with fallback TPs).

Outputs:
- CSV: mae_trades.csv with per-trade details
- Console summary table

Requires: pip install requests
"""

import csv, math, time, requests
from datetime import datetime, timezone, timedelta
from statistics import mean, pstdev

COINS = [("BTCUSDT","BTC", 0.0227), ("ETHUSDT","ETH", 0.0167), ("SOLUSDT","SOL", 0.0444)]
CONF_TRIGGER = 77
SL = 0.03                 # shown for reference; we don't stop on SL here (you execute manually)
HOLD_BARS = 4             # 96h
BASES = [
    "https://api.binance.com",
    "https://api1.binance.com",
    "https://api2.binance.com",
    "https://api3.binance.com",
    "https://data-api.binance.vision",
]
HEADERS = {"User-Agent": "mae-hourly/1.0 (+github actions)"}

START_DATE = datetime(2023,1,1,tzinfo=timezone.utc)

def binance_klines(symbol, interval, limit=1500, end_time_ms=None, start_time_ms=None):
    params={"symbol":symbol,"interval":interval,"limit":limit}
    if end_time_ms is not None: params["endTime"]=end_time_ms
    if start_time_ms is not None: params["startTime"]=start_time_ms
    last_err=None
    for base in BASES:
        try:
            r=requests.get(f"{base}/api/v3/klines", params=params, headers=HEADERS, timeout=30)
            r.raise_for_status()
            return r.json()
        except Exception as e:
            last_err=e; time.sleep(0.15); continue
    raise last_err if last_err else RuntimeError("All Binance bases failed")

def fully_closed_daily(symbol, limit=1500):
    # Only completed daily candles: end at today's 00:00 UTC - 1ms
    now=datetime.utcnow().replace(tzinfo=timezone.utc)
    midnight=datetime(now.year, now.month, now.day, tzinfo=timezone.utc)
    end_ms=int(midnight.timestamp()*1000)-1
    ks=binance_klines(symbol,"1d",limit,end_time_ms=end_ms)
    rows=[]
    for k in ks:
        close_ts=int(k[6])//1000
        rows.append((datetime.utcfromtimestamp(close_ts).replace(tzinfo=timezone.utc), float(k[4])))
    return rows

def pct_returns(closes):
    return [closes[i]/closes[i-1]-1.0 for i in range(1,len(closes))]

def daily_heat_from_series(closes, look=20):
    rets=pct_returns(closes)
    heats=[None]*len(closes)
    for i in range(1,len(closes)):
        if i<look: continue
        w=rets[i-look:i]
        mu=mean(w); sd=pstdev(w) if len(w)>1 else 0.0
        if sd<=0: continue
        last_ret=rets[i-1]
        z=(last_ret-mu)/sd
        z_signed = z if last_ret>0 else -z
        level = max(0,min(100, round(50+20*z_signed)))
        heats[i]=level
    return heats

def fetch_hourlies(symbol, start_dt, end_dt):
    out=[]
    start_ms=int(start_dt.timestamp()*1000)
    # Fetch in chunks
    while True:
        part=binance_klines(symbol,"1h",1500,start_time_ms=start_ms)
        if not part: break
        for k in part:
            ct=int(k[6])//1000
            if ct/1000.0: pass
            dt=datetime.utcfromtimestamp(ct).replace(tzinfo=timezone.utc)
            px=float(k[4])
            out.append((dt,px))
        start_ms=int(part[-1][6])+1
        if len(part)<1500: break
        if out[-1][0] >= end_dt + timedelta(hours=1):
            break
    # keep only within [start_dt, end_dt]
    return [(dt,px) for (dt,px) in out if start_dt <= dt <= end_dt]

def run():
    # 1) Build daily signal stream with priority (BTC>ETH>SOL), one trade at a time
    # Prepare per-coin daily series
    daily = {}
    for symbol,sym,_ in COINS:
        rows = fully_closed_daily(symbol)
        # filter by start date
        rows = [r for r in rows if r[0] >= START_DATE]
        dts, closes = zip(*rows)
        heats = daily_heat_from_series(list(closes), look=20)
        daily[sym] = {"symbol":symbol,"dts":dts,"cl":closes,"heat":heats}

    # Derive aligned calendar of daily closes (intersection of dates)
    all_dates = set(daily["BTC"]["dts"]) & set(daily["ETH"]["dts"]) & set(daily["SOL"]["dts"])
    cal = sorted([d for d in all_dates if d >= START_DATE])

    trades=[]
    in_pos=False
    # When we "enter", we anchor at that day's close (cal[i] date close) and then hold up to +96h
    for i in range(len(cal)):
        day = cal[i]
        # skip until we have at least 20-day history (heats computed)
        cond={}
        for sym in ["BTC","ETH","SOL"]:
            # find index of this day in that coin
            idx = daily[sym]["dts"].index(day)
            lvl = daily[sym]["heat"][idx]
            cond[sym]=lvl

        if not in_pos:
            # look for triggers this day
            cands=[]
            for sym in ["BTC","ETH","SOL"]:  # priority order
                lvl=cond[sym]
                if lvl is None: continue
                if lvl>=CONF_TRIGGER or lvl<=100-CONF_TRIGGER:
                    direction = "SHORT" if lvl>=CONF_TRIGGER else "LONG"
                    # entry price is that day's close (anchor)
                    entry_price = daily[sym]["cl"][daily[sym]["dts"].index(day)]
                    tp_pct = next(tp for (_sym,t, tp) in COINS if t==sym)
                    trades.append({
                        "sym": sym,
                        "direction": direction,
                        "anchor_day": day,
                        "entry_px": entry_price,
                        "tp_pct": tp_pct,
                        "exit_dt": None,
                        "exit_px": None,
                        "exit_reason": None,
                        "mae_pct": None,
                        "mfe_pct": None,
                        "hold_h": None
                    })
                    in_pos=True
                    break  # take first by priority
        else:
            # already in a trade → do nothing (no overlapping)
            pass

        # if we just entered, or previously in a trade, check if it should exit using hourly path
        if in_pos:
            tr = trades[-1]
            sym = tr["sym"]
            symbol = daily[sym]["symbol"]
            entry_dt = tr["anchor_day"]          # entry at daily close timestamp
            # define holding window end
            valid_until = entry_dt + timedelta(days=HOLD_BARS)
            # get hourlies from entry to valid_until
            hourlies = fetch_hourlies(symbol, entry_dt, valid_until)
            if not hourlies:
                continue
            entry_px = tr["entry_px"]
            tp_pct = tr["tp_pct"]
            # Track MAE/MFE in UNDERLYING terms
            mae = 0.0
            mfe = 0.0
            exited=False
            exit_dt = hourlies[-1][0]
            exit_px = hourlies[-1][1]
            exit_reason="expiry"

            # compute targets in price
            if tr["direction"]=="LONG":
                tp_price = entry_px*(1+tp_pct)
            else:
                tp_price = entry_px*(1-tp_pct)

            for (dt,px) in hourlies:
                # move (underlying)
                move = (px/entry_px - 1.0) if tr["direction"]=="LONG" else (entry_px/px - 1.0)
                # MAE/MFE
                mfe = max(mfe, move)
                mae = min(mae, move)  # most negative
                # TP check
                if tr["direction"]=="LONG" and px >= tp_price:
                    exit_dt, exit_px, exit_reason = dt, px, "tp"
                    exited=True
                    break
                if tr["direction"]=="SHORT" and px <= tp_price:
                    exit_dt, exit_px, exit_reason = dt, px, "tp"
                    exited=True
                    break

            # Fill trade results
            tr["exit_dt"]=exit_dt
            tr["exit_px"]=exit_px
            tr["exit_reason"]=exit_reason
            tr["hold_h"]=int(round((exit_dt - entry_dt).total_seconds()/3600))
            tr["mae_pct"]=mae*100.0
            tr["mfe_pct"]=mfe*100.0

            in_pos=False  # free slot for next signal

    # 2) Write CSV
    fname="mae_trades.csv"
    with open(fname,"w",newline="") as f:
        w=csv.writer(f)
        w.writerow(["#","Symbol","Direction","EntryTime(UTC)","EntryPx","ExitTime(UTC)","ExitPx","ExitReason",
                    "HoldHours","MAE% (adverse)","MFE% (favorable)","TP%"])
        for i,tr in enumerate(trades, start=1):
            w.writerow([
                i, tr["sym"], tr["direction"],
                tr["anchor_day"].strftime("%Y-%m-%d %H:%M"),
                f"{tr['entry_px']:.6f}",
                tr["exit_dt"].strftime("%Y-%m-%d %H:%M") if tr["exit_dt"] else "",
                f"{tr['exit_px']:.6f}" if tr["exit_px"] else "",
                tr["exit_reason"],
                tr["hold_h"],
                f"{tr['mae_pct']:.2f}",
                f"{tr['mfe_pct']:.2f}",
                f"{tr['tp_pct']*100:.2f}",
            ])

    # 3) Console summary
    print("\nPer-trade MAE/MFE (underlying %) — hourly path")
    print(f"Total trades: {len(trades)}\n")
    print(f"{'#':>3}  {'SYM':<3}  {'Dir':<5}  {'Entry UTC':<16}  {'Exit UTC':<16}  {'Holdh':>5}  {'MAE%':>7}  {'MFE%':>7}  {'Exit'}")
    for i,tr in enumerate(trades, start=1):
        print(f"{i:>3}  {tr['sym']:<3}  {tr['direction']:<5}  {tr['anchor_day'].strftime('%Y-%m-%d %H:%M'):16}  "
              f"{tr['exit_dt'].strftime('%Y-%m-%d %H:%M'):16}  {tr['hold_h']:5d}  {tr['mae_pct']:7.2f}  {tr['mfe_pct']:7.2f}  {tr['exit_reason']}")

    print(f"\nWrote CSV: {fname}")
    # Done
if __name__=="__main__":
    run()
