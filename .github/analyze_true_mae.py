#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import time, requests
from statistics import mean, pstdev, median
from datetime import datetime, timezone, timedelta

# ---------------- Config ----------------
COINS = [
    ("BTCUSDT","BTC",0.0227),
    ("ETHUSDT","ETH",0.0167),
    ("SOLUSDT","SOL",0.0444),
]

CONF_TRIGGER = 77
LOOKBACK     = 20
HOLD_BARS    = 4
START_DATE   = datetime(2023,1,1,tzinfo=timezone.utc)

BASES = [
    "https://data-api.binance.vision",
    "https://api.binance.com",
    "https://api1.binance.com",
    "https://api2.binance.com",
    "https://api3.binance.com",
]
HEADERS = {"User-Agent":"mae-analyzer/1.5 (+bbot)"}

# ---------------- Helpers ----------------
def binance_klines(symbol, interval, limit=1500, end_time_ms=None, start_time_ms=None):
    params={"symbol":symbol,"interval":interval,"limit":limit}
    if end_time_ms: params["endTime"]=end_time_ms
    if start_time_ms: params["startTime"]=start_time_ms
    for base in BASES:
        try:
            r=requests.get(f"{base}/api/v3/klines", params=params, headers=HEADERS, timeout=30)
            if r.status_code in (451,403): continue
            r.raise_for_status()
            return r.json()
        except Exception:
            continue
    raise RuntimeError("All Binance bases failed")

def fully_closed_daily(symbol):
    now=datetime.utcnow().replace(tzinfo=timezone.utc)
    y=now-timedelta(days=1)
    end_ms=int(datetime(y.year,y.month,y.day,23,59,59,tzinfo=timezone.utc).timestamp()*1000)+999
    ks=binance_klines(symbol,"1d",1500,end_time_ms=end_ms)
    rows=[]
    for k in ks:
        ct=int(k[6])//1000
        rows.append((datetime.utcfromtimestamp(ct).replace(tzinfo=timezone.utc), float(k[4])))
    return [(dt,px) for dt,px in rows if dt>=START_DATE]

def hourlies_between(symbol,start_dt,end_dt):
    out=[]
    start_ms=int(start_dt.timestamp()*1000)
    hard_end_ms=int((end_dt+timedelta(hours=1)).timestamp()*1000)
    while True:
        part=binance_klines(symbol,"1h",1500,start_time_ms=start_ms)
        if not part: break
        for k in part:
            ct=int(k[6])//1000
            dt=datetime.utcfromtimestamp(ct).replace(tzinfo=timezone.utc)
            if dt>end_dt: break
            out.append((dt,float(k[4])))
        start_ms=int(part[-1][6])+1
        if len(part)<1500 or start_ms>hard_end_ms: break
    ded={dt:px for dt,px in out}
    return sorted(ded.items())

def pct_returns(closes):
    return [closes[i]/closes[i-1]-1 for i in range(1,len(closes))]

def heat_series(closes, look=20):
    r=pct_returns(closes)
    out=[None]*len(closes)
    for i in range(len(closes)):
        if i<look or i>=len(closes)-1: continue
        w=r[i-look+1:i+1]
        mu,sd=mean(w),pstdev(w)
        if sd<=0: continue
        z=(r[i]-mu)/sd
        z_signed=z if r[i]>0 else -z
        out[i]=max(0,min(100,round(50+20*z_signed)))
    return out

# ---------------- Core ----------------
def run():
    daily={}
    for symbol,sym,tp_fb in COINS:
        rows=fully_closed_daily(symbol)
        dts,cls=zip(*rows)
        heats=heat_series(list(cls),look=LOOKBACK)
        daily[sym]={"symbol":symbol,"dates":list(dts),"closes":list(cls),
                    "heat":heats,"idx":{d:i for i,d in enumerate(dts)},"tp_fb":tp_fb}
        time.sleep(0.1)

    common=set.intersection(*(set(d["dates"]) for d in daily.values()))
    cal=[d for d in sorted(common) if all(daily[s]["heat"][daily[s]["idx"][d]] is not None for s in ("BTC","ETH","SOL"))]

    prior_mfe={s:[] for s in ("BTC","ETH","SOL")}
    def adaptive_tp(sym,fb):
        v=prior_mfe[sym]
        return (median(v)/100.0) if len(v)>=5 else fb

    def edge(sym,day):
        c=daily[sym]; i=c["idx"][day]
        if i==0: return None
        t,y=c["heat"][i],c["heat"][i-1]
        if t>=CONF_TRIGGER and y<CONF_TRIGGER: return "SHORT"
        if t<=100-CONF_TRIGGER and y>100-CONF_TRIGGER: return "LONG"
        return None

    trades=[]; active=None
    for day in cal:
        # check for active trade closure
        if active:
            sym=active["sym"]; s=daily[sym]
            entry_dt=active["entry_dt"]; entry_px=active["entry_px"]
            tp_pct=active["tp_pct"]; direction=active["direction"]
            valid_until=entry_dt+timedelta(days=HOLD_BARS)
            hours=hourlies_between(s["symbol"],entry_dt,valid_until)
            mae=mfe=0.0; exit_dt=valid_until; exit_px=entry_px; reason="expiry"
            if hours:
                tp_px=entry_px*(1+tp_pct) if direction=="LONG" else entry_px*(1-tp_pct)
                for dt,px in hours:
                    move=(px/entry_px-1) if direction=="LONG" else (entry_px/px-1)
                    mfe=max(mfe,move); mae=min(mae,move)
                    if (direction=="LONG" and px>=tp_px) or (direction=="SHORT" and px<=tp_px):
                        exit_dt,exit_px,reason=dt,px,"tp"; break
            roi=(exit_px/entry_px-1) if direction=="LONG" else (entry_px/exit_px-1)
            trades.append({"sym":sym,"direction":direction,"entry_dt":entry_dt,"exit_dt":exit_dt,
                           "entry_px":entry_px,"exit_px":exit_px,"mae_pct":mae*100,"mfe_pct":mfe*100,
                           "roi_pct":roi*100,"reason":reason})
            prior_mfe[sym].append(mfe*100)
            active=None

        # only open new if none active
        if not active:
            candidates=[]
            for sym in ("BTC","ETH","SOL"):
                d=edge(sym,day)
                if d: candidates.append((sym,d))
            if candidates:
                # same priority rule
                sym,dirn=min(candidates,key=lambda x:("BTC","ETH","SOL").index(x[0]))
                i=daily[sym]["idx"][day]
                entry_px=daily[sym]["closes"][i]
                tp_pct=adaptive_tp(sym,daily[sym]["tp_fb"])
                active={"sym":sym,"direction":dirn,"entry_dt":day,"entry_px":entry_px,"tp_pct":tp_pct}

    # ---------------- Output ----------------
    print(f"\n=== True MAE/MFE for real trades ===\n")
    print(f"{'#':>3} {'SYM':<3} {'Dir':<5} {'Entry UTC':<16} {'Exit UTC':<16} {'MAE%':>7} {'MFE%':>7} {'ROI%':>7} {'Exit'}")
    for i,t in enumerate(trades,1):
        print(f"{i:>3} {t['sym']:<3} {t['direction']:<5} "
              f"{t['entry_dt'].strftime('%Y-%m-%d %H:%M'):16} "
              f"{t['exit_dt'].strftime('%Y-%m-%d %H:%M'):16} "
              f"{t['mae_pct']:7.2f} {t['mfe_pct']:7.2f} {t['roi_pct']:7.2f} {t['reason']}")
    maes=[t["mae_pct"] for t in trades]; mfes=[t["mfe_pct"] for t in trades]; rois=[t["roi_pct"] for t in trades]
    print("\nSummary:")
    print(f"Trades: {len(trades)}")
    print(f"Avg MAE: {mean(maes):.2f}% | Med MAE: {median(maes):.2f}%")
    print(f"Avg MFE: {mean(mfes):.2f}% | Med MFE: {median(mfes):.2f}%")
    print(f"Avg ROI: {mean(rois):.2f}% | Med ROI: {median(rois):.2f}%")

if __name__=="__main__":
    run()
        
