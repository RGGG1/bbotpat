#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import time, requests
from statistics import median
from datetime import datetime, timezone, timedelta

# ---------------- Config (44× rules) ----------------
COINS = [("BTCUSDT","BTC",0.0227), ("ETHUSDT","ETH",0.0167), ("SOLUSDT","SOL",0.0444)]
Z_THRESH   = 2.5         # |z| >= 2.5 on 20-day daily-return z
LOOKBACK   = 20
HOLD_DAYS  = 4           # 96h
START_DATE = datetime(2023,1,1,tzinfo=timezone.utc)
SL         = 0.03        # only used for info; no SL in PnL here

BASES = [
    "https://data-api.binance.vision", "https://api.binance.com",
    "https://api1.binance.com", "https://api2.binance.com", "https://api3.binance.com",
]
HEADERS = {"User-Agent":"backtest-44x/1.0 (+bbot)"}

# ---------------- HTTP helpers ----------------
def _req(base, path, params):
    r = requests.get(f"{base}{path}", params=params, headers=HEADERS, timeout=30)
    if r.status_code in (451,403): raise requests.HTTPError(f"{r.status_code} {r.reason}")
    r.raise_for_status(); return r.json()

def binance_klines(symbol, interval, limit=1500, end_time_ms=None, start_time_ms=None, tries=6):
    params={"symbol":symbol,"interval":interval,"limit":limit}
    if end_time_ms is not None: params["endTime"]=end_time_ms
    if start_time_ms is not None: params["startTime"]=start_time_ms
    last=None; back=0.25
    for _ in range(tries):
        for b in BASES:
            try: return _req(b,"/api/v3/klines",params)
            except Exception as e: last=e; time.sleep(back)
        back=min(2.0, back*1.8)
    raise last if last else RuntimeError("All bases failed")

def fully_closed_daily(symbol):
    now = datetime.utcnow().replace(tzinfo=timezone.utc)
    y   = now - timedelta(days=1)
    end_ms = int(datetime(y.year,y.month,y.day,23,59,59,tzinfo=timezone.utc).timestamp()*1000) + 999
    ks = binance_klines(symbol,"1d",1500,end_time_ms=end_ms)
    rows=[]
    for k in ks:
        ct=int(k[6])//1000
        dt=datetime.utcfromtimestamp(ct).replace(tzinfo=timezone.utc)
        rows.append((dt.date(), float(k[4])))
    return [(d,p) for (d,p) in rows if datetime(d.year,d.month,d.day,tzinfo=timezone.utc)>=START_DATE]

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
            out.append((dt, float(k[2]), float(k[3]), float(k[4])))  # high, low, close
        start_ms=int(part[-1][6])+1
        if len(part)<1500 or start_ms>hard_end_ms or (out and out[-1][0]>=end_dt): break
    # unique by dt
    ded={dt:(hi,lo,cl) for dt,hi,lo,cl in out}
    out=sorted((dt,*ded[dt]) for dt in ded.keys())
    return out

# ---------------- Math helpers ----------------
def pct_returns(closes): return [closes[i]/closes[i-1]-1.0 for i in range(1,len(closes))]

def today_z_signed(closes, look=20):
    r=pct_returns(closes)
    if len(r)<look: return None
    w=r[-look:]
    mu=sum(w)/look
    var=sum((x-mu)**2 for x in w)/look
    sd=var**0.5 if var>0 else 0.0
    if sd<=0: return None
    return (r[-1]-mu)/sd

def adaptive_tp(sym, history, fb):
    vals=[x["mfe_pct"] for x in history if x.get("mfe_pct") is not None]
    return (median(vals)/100.0) if len(vals)>=5 else fb

# ---------------- Backtest ----------------
def run():
    # Load daily data
    daily={}
    for symbol,sym,fb in COINS:
        rows=fully_closed_daily(symbol)
        dts,closes=zip(*rows)
        daily[sym]={
            "symbol":symbol,"dates":list(dts),"closes":list(closes),
            "idx":{d:i for i,d in enumerate(dts)},
        }
        time.sleep(0.1)

    # calendar where all three exist
    cal=set(daily["BTC"]["dates"]) & set(daily["ETH"]["dates"]) & set(daily["SOL"]["dates"])
    cal=sorted(d for d in cal if datetime(d.year,d.month,d.day,tzinfo=timezone.utc)>=START_DATE)

    trades=[]; active=None; equity=1.0  # single bankroll, compounding
    per_sym_hist={"BTC":[], "ETH":[], "SOL":[]}

    for day in cal:
        # close active if exists (check TP intraday or expire at 96h)
        if active is not None:
            sym=active["sym"]; symbol=daily[sym]["symbol"]
            entry_dt=active["entry_dt"]; entry_px=active["entry_px"]
            tp_pct=active["tp_pct"]; direction=active["direction"]
            valid_until=entry_dt + timedelta(days=HOLD_DAYS)
            bars=hourlies_between(symbol, entry_dt, valid_until)

            mae=0.0; mfe=0.0
            exit_dt=valid_until; exit_px=entry_px; reason="expiry"

            if direction=="LONG":
                tp_px=entry_px*(1+tp_pct)
                for dt,hi,lo,cl in bars:
                    mae=min(mae, lo/entry_px-1.0)
                    mfe=max(mfe, hi/entry_px-1.0)
                    if hi>=tp_px:
                        exit_dt,exit_px,reason=dt,tp_px,"tp"; break
                roi=(exit_px/entry_px-1.0)
            else:
                tp_px=entry_px*(1-tp_pct)
                for dt,hi,lo,cl in bars:
                    mae=min(mae, entry_px/hi-1.0)   # adverse = price goes up
                    mfe=max(mfe, entry_px/lo-1.0)
                    if lo<=tp_px:
                        exit_dt,exit_px,reason=dt,tp_px,"tp"; break
                roi=(entry_px/exit_px-1.0)

            equity *= (1.0 + roi)

            tr={"sym":sym,"direction":direction,"entry_dt":entry_dt,"exit_dt":exit_dt,
                "entry_px":entry_px,"exit_px":exit_px,"hold_h":int(round((exit_dt-entry_dt).total_seconds()/3600)),
                "mae_pct":mae*100.0,"mfe_pct":mfe*100.0,"roi_pct":roi*100.0,"reason":reason,"equity":equity}
            trades.append(tr)
            per_sym_hist[sym].append({"mfe_pct":mfe*100.0})
            active=None  # free to look for next

        # if free, check today’s edges and open at most ONE (BTC>ETH>SOL)
        if active is None:
            candidates=[]
            for sym in ("BTC","ETH","SOL"):
                i=daily[sym]["idx"][day]
                closes=daily[sym]["closes"][:i+1]
                z=today_z_signed(closes, LOOKBACK)
                if z is None: continue
                if abs(z) >= Z_THRESH:
                    direction = "SHORT" if z>0 else "LONG"
                    entry_px = closes[-1]
                    entry_dt = datetime(day.year,day.month,day.day,tzinfo=timezone.utc) + timedelta(hours=23, minutes=59)
                    tp_pct  = adaptive_tp(sym, per_sym_hist[sym],
                                          next(fb for (_sym1,_sym2,fb) in COINS if _sym2==sym))
                    candidates.append((sym,direction,entry_px,entry_dt,tp_pct))
            if candidates:
                order = {"BTC":0,"ETH":1,"SOL":2}
                sym,dirn,px,dt,tp = sorted(candidates,key=lambda x:order[x[0]])[0]
                active={"sym":sym,"direction":dirn,"entry_px":px,"entry_dt":dt,"tp_pct":tp}

    # ------------- Print results -------------
    print("\n=== 44× Strategy (|z|>=2.5, single active, compounding; since 2023-01-01) ===\n")
    print(f"{'#':>3}  {'SYM':<3}  {'Dir':<5}  {'Entry UTC':<16}  {'Exit UTC':<16}  {'Hh':>4}  "
          f"{'Entry':>12}  {'Exit':>12}  {'MAE%':>7}  {'MFE%':>7}  {'ROI%':>7}  {'Equity×':>8}  {'Exit'}")
    for i,tr in enumerate(trades,1):
        print(f"{i:>3}  {tr['sym']:<3}  {tr['direction']:<5}  "
              f"{tr['entry_dt'].strftime('%Y-%m-%d %H:%M'):16}  "
              f"{tr['exit_dt'].strftime('%Y-%m-%d %H:%M'):16}  "
              f"{tr['hold_h']:4d}  "
              f"{tr['entry_px']:12.4f}  {tr['exit_px']:12.4f}  "
              f"{tr['mae_pct']:7.2f}  {tr['mfe_pct']:7.2f}  {tr['roi_pct']:7.2f}  "
              f"{tr['equity']:8.2f}  {tr['reason']}")
    if trades:
        print(f"\nTrades: {len(trades)}  |  Final equity multiple: {trades[-1]['equity']:.2f}×")
