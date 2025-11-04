#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Analyze true MAE/MFE for the *actual trades* the live algo would have taken:
- BTC > ETH > SOL priority
- Only one trade active at a time
- Entry on daily 77% signal (anchor = daily close)
- Exit on TP hit or 96h expiry
- Hourly data used to compute MAE (worst move) and MFE (best move)

Outputs mae_true.csv (~29 trades).
"""

import csv, time, requests
from datetime import datetime, timezone, timedelta
from statistics import mean, pstdev

COINS = [("BTCUSDT","BTC",0.0227), ("ETHUSDT","ETH",0.0167), ("SOLUSDT","SOL",0.0444)]
CONF_TRIGGER=77
HOLD_BARS=4
BASES=[
    "https://api.binance.com",
    "https://api1.binance.com",
    "https://api2.binance.com",
    "https://api3.binance.com",
]
HEADERS={"User-Agent":"true-mae/1.0"}
START_DATE=datetime(2023,1,1,tzinfo=timezone.utc)

def binance_klines(symbol, interval, limit=1500, end_time_ms=None, start_time_ms=None):
    p={"symbol":symbol,"interval":interval,"limit":limit}
    if end_time_ms:p["endTime"]=end_time_ms
    if start_time_ms:p["startTime"]=start_time_ms
    for base in BASES:
        try:
            r=requests.get(f"{base}/api/v3/klines",params=p,headers=HEADERS,timeout=30)
            r.raise_for_status();return r.json()
        except:time.sleep(0.2)
    raise RuntimeError("All Binance bases failed")

def daily_data(symbol):
    now=datetime.utcnow().replace(tzinfo=timezone.utc)
    end_ms=int(datetime(now.year,now.month,now.day,tzinfo=timezone.utc).timestamp()*1000)-1
    ks=binance_klines(symbol,"1d",1500,end_time_ms=end_ms)
    out=[(datetime.utcfromtimestamp(int(k[6])//1000).replace(tzinfo=timezone.utc),float(k[4])) for k in ks]
    return out

def pct_returns(c):return [c[i]/c[i-1]-1 for i in range(1,len(c))]

def heat_series(c,look=20):
    r=pct_returns(c);out=[None]*len(c)
    for i in range(len(c)):
        if i<look:continue
        w=r[i-look:i];mu=mean(w);sd=pstdev(w) if len(w)>1 else 0
        if sd<=0:continue
        z=(r[i-1]-mu)/sd
        z_signed=z if r[i-1]>0 else -z
        out[i]=max(0,min(100,round(50+20*z_signed)))
    return out

def hourlies(symbol,start,end):
    start_ms=int(start.timestamp()*1000)
    out=[]
    while True:
        part=binance_klines(symbol,"1h",1500,start_time_ms=start_ms)
        if not part:break
        for k in part:
            dt=datetime.utcfromtimestamp(int(k[6])//1000).replace(tzinfo=timezone.utc)
            px=float(k[4])
            out.append((dt,px))
        start_ms=int(part[-1][6])+1
        if len(part)<1500 or out[-1][0]>=end:break
    return [x for x in out if start<=x[0]<=end]

def run():
    daily={}
    for s,t,tp in COINS:
        rows=daily_data(s);rows=[r for r in rows if r[0]>=START_DATE]
        dts,cl=zip(*rows)
        heat=heat_series(list(cl))
        daily[t]={"symbol":s,"dts":dts,"cl":cl,"heat":heat}
        time.sleep(0.3)
    # calendar intersection
    cal=sorted(set(daily["BTC"]["dts"])&set(daily["ETH"]["dts"])&set(daily["SOL"]["dts"]))
    trades=[]
    active=None
    for d in cal:
        # index for each coin
        cond={}
        for t in ["BTC","ETH","SOL"]:
            idx=daily[t]["dts"].index(d)
            cond[t]=daily[t]["heat"][idx]
        if not active:
            for t in ["BTC","ETH","SOL"]:
                lvl=cond[t]
                if lvl is None:continue
                if lvl>=CONF_TRIGGER or lvl<=100-CONF_TRIGGER:
                    dirn="SHORT" if lvl>=CONF_TRIGGER else "LONG"
                    entry=daily[t]["cl"][daily[t]["dts"].index(d)]
                    tp=[tp for _,tt,tp in COINS if tt==t][0]
                    active={"sym":t,"symbol":daily[t]["symbol"],"direction":dirn,
                            "entry_px":entry,"entry_dt":d,"tp":tp}
                    break
        elif active:
            # skip days until exit
            sym=active["sym"];symbol=active["symbol"]
            entry_dt=active["entry_dt"];entry_px=active["entry_px"];tp=active["tp"]
            valid_until=entry_dt+timedelta(days=HOLD_BARS)
            h=hourlies(symbol,entry_dt,valid_until)
            mae,mfe=0,0;exit_dt=h[-1][0];exit_px=h[-1][1];reason="expiry"
            tp_px=entry_px*(1+tp) if active["direction"]=="LONG" else entry_px*(1-tp)
            for (dt,px) in h:
                move=(px/entry_px-1) if active["direction"]=="LONG" else (entry_px/px-1)
                mfe=max(mfe,move);mae=min(mae,move)
                if active["direction"]=="LONG" and px>=tp_px:
                    exit_dt,exit_px,reason=dt,px,"tp";break
                if active["direction"]=="SHORT" and px<=tp_px:
                    exit_dt,exit_px,reason=dt,px,"tp";break
            trades.append({
                "sym":sym,"direction":active["direction"],"entry_dt":entry_dt,
                "exit_dt":exit_dt,"entry_px":entry_px,"exit_px":exit_px,
                "hold_h":int((exit_dt-entry_dt).total_seconds()/3600),
                "mae":mae*100,"mfe":mfe*100,"reason":reason
            })
            active=None  # release

    with open("mae_true.csv","w",newline="") as f:
        w=csv.writer(f)
        w.writerow(["#","Symbol","Dir","EntryUTC","ExitUTC","HoldH","MAE%","MFE%","Reason"])
        for i,tr in enumerate(trades,1):
            w.writerow([i,tr["sym"],tr["direction"],
                        tr["entry_dt"].strftime("%Y-%m-%d %H:%M"),
                        tr["exit_dt"].strftime("%Y-%m-%d %H:%M"),
                        tr["hold_h"],f"{tr['mae']:.2f}",f"{tr['mfe']:.2f}",tr["reason"]])
    print(f"Wrote mae_true.csv with {len(trades)} trades")
    for i,tr in enumerate(trades,1):
        print(f"{i:02d} {tr['sym']} {tr['direction']}  MAE={tr['mae']:.2f}%  MFE={tr['mfe']:.2f}%  {tr['reason']}")
if __name__=="__main__":
    run()
      
