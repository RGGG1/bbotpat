#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Backtest for the "44×" setup (edge-cross 90/10, BTC>ETH>SOL, single bankroll, compounding).
- Entry: edge-cross into >=90 (SHORT) or <=10 (LONG) today vs yesterday (heat from 20d z of daily returns).
- Exit: TP at per-coin adaptive median MFE (fallbacks until >=5), else expiry at 96h.
- One trade at a time. If exit and new entry happen on the same daily close, both occur (no overlap).
- Intratrade MAE/MFE measured on hourly CLOSES between entry and exit (or until expiry).

Outputs:
- Table of trades with Entry/Exit UTC, TP%, MAE/MFE%, ROI%, and compounded equity after each trade.
"""

import time, requests, csv
from statistics import mean, pstdev, median
from datetime import datetime, timezone, timedelta

COINS = [("BTCUSDT","BTC",0.0227), ("ETHUSDT","ETH",0.0167), ("SOLUSDT","SOL",0.0444)]
LOOKBACK=20
HEAT_LONG, HEAT_SHORT = 10, 90
HOLD_BARS = 4
START_DATE = datetime(2023,1,1,tzinfo=timezone.utc)

BASES = [
    "https://data-api.binance.vision",
    "https://api.binance.com",
    "https://api1.binance.com",
    "https://api2.binance.com",
    "https://api3.binance.com",
]
HEADERS={"User-Agent":"analyze-44x/1.0 (+github actions)"}

def binance_klines(symbol, interval, limit=1500, end_time_ms=None, start_time_ms=None, tries=6):
    params={"symbol":symbol,"interval":interval,"limit":limit}
    if end_time_ms is not None: params["endTime"]=end_time_ms
    if start_time_ms is not None: params["startTime"]=start_time_ms
    last_err=None; backoff=0.25
    for _ in range(tries):
        for base in BASES:
            try:
                r=requests.get(f"{base}/api/v3/klines", params=params, headers=HEADERS, timeout=30)
                if r.status_code in (451,403):
                    last_err = requests.HTTPError(f"{r.status_code} {r.reason}")
                    continue
                r.raise_for_status()
                return r.json()
            except Exception as e:
                last_err=e; time.sleep(backoff)
        backoff=min(2.0, backoff*1.8)
    raise last_err if last_err else RuntimeError("All Binance bases failed")

def fully_closed_daily(symbol):
    now = datetime.utcnow().replace(tzinfo=timezone.utc)
    y   = now - timedelta(days=1)
    end_ms = int(datetime(y.year,y.month,y.day,23,59,59,tzinfo=timezone.utc).timestamp()*1000) + 999
    ks = binance_klines(symbol,"1d",1500,end_time_ms=end_ms)
    rows=[]
    for k in ks:
        ct=int(k[6])//1000
        dt=datetime.utcfromtimestamp(ct).replace(tzinfo=timezone.utc)
        rows.append((dt, float(k[4])))
    return [(dt,px) for (dt,px) in rows if dt >= START_DATE]

def hourlies_between(symbol, start_dt, end_dt):
    out=[]; start_ms=int(start_dt.timestamp()*1000)
    hard_end_ms=int((end_dt+timedelta(hours=1)).timestamp()*1000)
    while True:
        part=binance_klines(symbol,"1h",1500,start_time_ms=start_ms)
        if not part: break
        for k in part:
            ct=int(k[6])//1000
            dt=datetime.utcfromtimestamp(ct).replace(tzinfo=timezone.utc)
            if dt> end_dt: break
            out.append((dt, float(k[4])))
        start_ms=int(part[-1][6])+1
        if len(part)<1500 or start_ms>hard_end_ms or (out and out[-1][0]>=end_dt):
            break
    # dedupe by dt
    return sorted({dt:px for dt,px in out}.items())

def pct_returns(closes): return [closes[i]/closes[i-1]-1.0 for i in range(1,len(closes))]

def heat_series_aligned_to_day(closes, look=LOOKBACK):
    r=pct_returns(closes)
    out=[None]*len(closes)
    for i in range(len(closes)):
        if i<look or i>=len(closes)-1: continue
        window = r[i-look+1:i+1]
        if len(window)!=look: continue
        mu=mean(window); sd=pstdev(window) if len(window)>1 else 0.0
        if sd<=0: continue
        last_ret=r[i]
        z=(last_ret-mu)/sd
        z_signed=z if last_ret>0 else -z
        out[i]=max(0, min(100, round(50+20*z_signed)))
    return out

def run():
    # Load daily series with heats
    daily={}
    for symbol,sym,fb in COINS:
        rows=fully_closed_daily(symbol)
        dts,cls=zip(*rows)
        heats=heat_series_aligned_to_day(list(cls), LOOKBACK)
        daily[sym]={"symbol":symbol,"dates":list(dts),"closes":list(cls),"heats":heats,"idx":{d:i for i,d in enumerate(dts)},"tp_fb":fb}
        time.sleep(0.1)

    # Common calendar where heats exist for all
    common=set(daily["BTC"]["dates"]) & set(daily["ETH"]["dates"]) & set(daily["SOL"]["dates"])
    cal=[]
    for d in sorted(common):
        ok=True
        for sym in ("BTC","ETH","SOL"):
            i=daily[sym]["idx"][d]
            if daily[sym]["heats"][i] is None or daily[sym]["heats"][i-1] is None:
                ok=False; break
        if ok: cal.append(d)

    # Adaptive TP memory (per coin, % values)
    prior_mfe={"BTC":[], "ETH":[], "SOL":[]}
    def adaptive_tp(sym, fb):
        vals=prior_mfe[sym]
        return (median(vals)/100.0) if len(vals)>=5 else fb

    def edge_dir(sym, day):
        coin=daily[sym]; i=coin["idx"][day]
        t=coin["heats"][i]; y=coin["heats"][i-1]
        # crossed extremes?
        crossed_short = (t>=HEAT_SHORT) and (y<HEAT_SHORT)
        crossed_long  = (t<=HEAT_LONG)  and (y>HEAT_LONG)
        if not (crossed_short or crossed_long): return None
        # contrarian direction from the sign of today's return
        r=pct_returns(coin["closes"])
        today_ret=r[i]  # aligned
        return "SHORT" if today_ret>0 else "LONG"

    equity=100.0
    trades=[]
    active=None

    for day in cal:
        # 1) If a position is open, simulate its completion (TP or expiry) BEFORE opening a new one
        if active is not None:
            sym=active["sym"]; symbol=daily[sym]["symbol"]
            entry_dt=active["entry_dt"]; entry_px=active["entry_px"]
            tp_pct=active["tp_pct"]; direction=active["direction"]
            valid_until = entry_dt + timedelta(days=HOLD_BARS)

            hours = hourlies_between(symbol, entry_dt, valid_until)
            mae = 0.0; mfe = 0.0
            exit_dt=valid_until; exit_px=entry_px; reason="expiry"
            if hours:
                tp_px = entry_px*(1+tp_pct) if direction=="LONG" else entry_px*(1-tp_pct)
                for dt,px in hours:
                    move = (px/entry_px-1.0) if direction=="LONG" else (entry_px/px-1.0)
                    mfe = max(mfe, move)
                    mae = min(mae, move)
                    hit_tp = (direction=="LONG" and px>=tp_px) or (direction=="SHORT" and px<=tp_px)
                    if hit_tp:
                        exit_dt, exit_px, reason = dt, px, "tp"
                        break
            roi = (exit_px/entry_px-1.0) if direction=="LONG" else (entry_px/exit_px-1.0)
            equity *= (1.0 + roi)
            trades.append({
                "sym":sym,"direction":direction,"entry_dt":entry_dt,"exit_dt":exit_dt,
                "entry_px":entry_px,"exit_px":exit_px,"tp_pct":tp_pct*100.0,
                "mae_pct":mae*100.0,"mfe_pct":mfe*100.0,"roi_pct":roi*100.0,
                "after":equity,"reason":reason
            })
            prior_mfe[sym].append(mfe*100.0)
            active=None  # free to open a new trade (possibly same day)

        # 2) If free, see if any coin triggers today; open one by BTC>ETH>SOL
        if active is None:
            cands=[]
            for sym in ("BTC","ETH","SOL"):
                dirn = edge_dir(sym, day)
                if dirn:
                    i = daily[sym]["idx"][day]
                    entry_px = daily[sym]["closes"][i]
                    tp_pct   = adaptive_tp(sym, daily[sym]["tp_fb"])
                    cands.append((sym, dirn, entry_px))
            if cands:
                cands.sort(key=lambda x: ("BTC","ETH","SOL").index(x[0]))
                sym,dirn,entry_px = cands[0]
                active = {
                    "sym":sym, "symbol":daily[sym]["symbol"], "direction":dirn,
                    "entry_dt":day, "entry_px":entry_px, "tp_pct":tp_pct
                }

    # Print results on screen
    print("\n=== Backtest (golden 44×) — single bankroll, compounded ===")
    print(f"Start balance: $100.00")
    print(f"{'#':>3}  {'SYM':<3}  {'Dir':<5}  {'Entry UTC':<16}  {'Exit UTC':<16}  {'TP%':>6}  {'MAE%':>7}  {'MFE%':>7}  {'ROI%':>7}  {'After$':>9}  {'Exit'}")
    for i,tr in enumerate(trades,1):
        print(f"{i:>3}  {tr['sym']:<3}  {tr['direction']:<5}  "
              f"{tr['entry_dt'].strftime('%Y-%m-%d %H:%M'):16}  "
              f"{tr['exit_dt'].strftime('%Y-%m-%d %H:%M'):16}  "
              f"{tr['tp_pct']:6.2f}  {tr['mae_pct']:7.2f}  {tr['mfe_pct']:7.2f}  {tr['roi_pct']:7.2f}  "
              f"{tr['after']:9.2f}  {tr['reason']}")
    if trades:
        rois=[t["roi_pct"] for t in trades]
        print(f"\nSummary: Trades: {len(trades)}  |  Final equity: {equity/100.0:.2f}×  (${equity:,.2f})")
        print(f"Avg ROI/trade: {mean(rois):.2f}%  |  Med ROI: {median(rois):.2f}%")

if __name__=="__main__":
    run()
