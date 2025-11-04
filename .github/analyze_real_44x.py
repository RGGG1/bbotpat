#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import requests, time
from statistics import mean, median, pstdev
from datetime import datetime, timezone, timedelta

COINS = [("BTCUSDT","BTC",0.0227),
         ("ETHUSDT","ETH",0.0167),
         ("SOLUSDT","SOL",0.0444)]

CONF_TRIGGER = 90
LOOKBACK = 20
HOLD_DAYS = 4
START_DATE = datetime(2023,1,1,tzinfo=timezone.utc)
BASES = [
    "https://data-api.binance.vision",
    "https://api.binance.com",
    "https://api1.binance.com",
    "https://api2.binance.com",
    "https://api3.binance.com",
]
HEADERS = {"User-Agent":"real44x/1.0 (+bbot)"}

def binance_klines(symbol,interval,limit=1500,end_time_ms=None,start_time_ms=None):
    params={"symbol":symbol,"interval":interval,"limit":limit}
    if end_time_ms: params["endTime"]=end_time_ms
    if start_time_ms: params["startTime"]=start_time_ms
    for base in BASES:
        try:
            r=requests.get(f"{base}/api/v3/klines",params=params,headers=HEADERS,timeout=20)
            if r.status_code==451: continue
            r.raise_for_status(); return r.json()
        except Exception: continue
    raise RuntimeError("binance_klines failed")

def fully_closed_daily(symbol):
    now=datetime.utcnow().replace(tzinfo=timezone.utc)
    y=now-timedelta(days=1)
    end_ms=int(datetime(y.year,y.month,y.day,23,59,59,tzinfo=timezone.utc).timestamp()*1000)+999
    ks=binance_klines(symbol,"1d",1500,end_time_ms=end_ms)
    rows=[(datetime.utcfromtimestamp(int(k[6])//1000).replace(tzinfo=timezone.utc),float(k[4])) for k in ks]
    return [(dt,px) for dt,px in rows if dt>=START_DATE]

def hourlies_between(symbol,start,end):
    out=[]; start_ms=int(start.timestamp()*1000)
    end_ms=int((end+timedelta(hours=1)).timestamp()*1000)
    while True:
        part=binance_klines(symbol,"1h",1500,start_time_ms=start_ms)
        if not part: break
        for k in part:
            dt=datetime.utcfromtimestamp(int(k[6])//1000).replace(tzinfo=timezone.utc)
            if dt>end: break
            out.append((dt,float(k[4])))
        start_ms=int(part[-1][6])+1
        if len(part)<1500 or out and out[-1][0]>=end: break
    ded={dt:px for dt,px in out}
    return sorted(ded.items())

def pct_returns(c):
    return [c[i]/c[i-1]-1 for i in range(1,len(c))]

def heat_series(closes,look=20):
    r=pct_returns(closes); out=[None]*len(closes)
    for i in range(len(closes)):
        if i<look or i>=len(closes)-1: continue
        w=r[i-look+1:i+1]; mu=sum(w)/look
        sd=(sum((x-mu)**2 for x in w)/look)**0.5
        if sd<=0: continue
        last=r[i]; z=(last-mu)/sd; z_signed=z if last>0 else -z
        out[i]=max(0,min(100,round(50+20*z_signed)))
    return out

def run():
    daily={}
    for symb,sym,tp in COINS:
        d=fully_closed_daily(symb)
        dts,cls=zip(*d)
        heats=heat_series(list(cls),LOOKBACK)
        daily[sym]={"symb":symb,"dates":list(dts),"closes":list(cls),
                    "heat":heats,"idx":{d:i for i,d in enumerate(dts)},"tp":tp}
        time.sleep(0.1)
    common=set(daily["BTC"]["dates"])&set(daily["ETH"]["dates"])&set(daily["SOL"]["dates"])
    cal=[d for d in sorted(common) if all(daily[s]["heat"][daily[s]["idx"][d]] for s in ("BTC","ETH","SOL"))]

    def edge(sym,d):
        c=daily[sym]; i=c["idx"][d]
        if i==0: return None
        y=c["heat"][i-1]; t=c["heat"][i]
        if y<CONF_TRIGGER<=t: return "SHORT"
        if y>100-CONF_TRIGGER>=t: return "LONG"
        return None

    prior_mfe={"BTC":[], "ETH":[], "SOL":[]}
    def tp(sym): 
        v=prior_mfe[sym]
        return (median(v)/100) if len(v)>=5 else daily[sym]["tp"]

    trades=[]; active=None
    for d in cal:
        # close
        if active:
            sym=active["sym"]; s=daily[sym]
            start=active["date"]; entry=active["px"]; tpp=active["tp"]; dirn=active["dir"]
            until=start+timedelta(days=HOLD_DAYS)
            hrs=hourlies_between(s["symb"],start,until)
            mae=mfe=0; exdt=until; expx=entry; reason="expiry"
            tp_px=entry*(1+tpp) if dirn=="LONG" else entry*(1-tpp)
            for dt,px in hrs:
                move=(px/entry-1) if dirn=="LONG" else (entry/px-1)
                mae=min(mae,move); mfe=max(mfe,move)
                if (dirn=="LONG" and px>=tp_px) or (dirn=="SHORT" and px<=tp_px):
                    exdt,expx,reason=dt,px,"tp"; break
            roi=(expx/entry-1) if dirn=="LONG" else (entry/expx-1)
            trades.append({"sym":sym,"dir":dirn,"entry":entry,"date":start,
                           "exit":exdt,"tp":tpp,"roi":roi,"mae":mae,"mfe":mfe,"reason":reason})
            prior_mfe[sym].append(mfe*100); active=None

        # open
        if not active:
            for sym in ("BTC","ETH","SOL"):
                ddir=edge(sym,d)
                if ddir:
                    i=daily[sym]["idx"][d]
                    active={"sym":sym,"dir":ddir,"px":daily[sym]["closes"][i],
                            "date":d,"tp":tp(sym)}
                    break

    # Print results
    eq=1.0
    print("\n=== 44× Strategy (90% edge trigger, compounding) ===\n")
    print(f"{'#':>2} {'SYM':<3} {'Dir':<5} {'Date':<10} {'Entry':>10} {'TP%':>6} {'ROI%':>7} {'Equity×':>8} {'Exit'}")
    for i,t in enumerate(trades,1):
        eq*=(1+t["roi"])
        print(f"{i:2d} {t['sym']:<3} {t['dir']:<5} {t['date'].date()} {t['entry']:>10.2f} "
              f"{t['tp']*100:6.2f} {t['roi']*100:7.2f} {eq:8.2f} {t['reason']}")
    rois=[x["roi"]*100 for x in trades]
    print("\nTrades:",len(trades),
          f"| Avg ROI {mean(rois):.2f}% | Median {median(rois):.2f}% | Final Equity {eq:.2f}×")

if __name__=="__main__":
    run()
