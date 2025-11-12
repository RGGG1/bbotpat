#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
v1.4 — z-score algo + status board + confidence%
"""

import os, json, time, math
from datetime import datetime, timezone, timedelta
from typing import List, Tuple
import requests

COINS = [("BTCUSDT","BTC"),("ETHUSDT","ETH"),("SOLUSDT","SOL")]
Z_THRESH = 2.5
SL = 0.03
HOLD_BARS = 4
STATE_FILE = "adaptive_alerts_state.json"
TP_FALLBACK = {"BTC":0.0227,"ETH":0.0167,"SOL":0.0444}

TG_BOT_TOKEN=os.getenv("TG_BOT_TOKEN")
TG_CHAT_ID=os.getenv("TG_CHAT_ID")
LABEL=os.getenv("ALERTS_NAME","BINANCE")

BASES=[
 "https://api.binance.com","https://api1.binance.com","https://api2.binance.com",
 "https://api3.binance.com","https://data-api.binance.vision"
]
HEADERS={"User-Agent":"alerts-bot/1.4 (+https://github.com)"}

def binance_daily(symbol:str)->List[Tuple[datetime.date,float]]:
    last=None
    for base in BASES:
        try:
            r=requests.get(f"{base}/api/v3/klines",
                params={"symbol":symbol,"interval":"1d","limit":1500},
                headers=HEADERS,timeout=30)
            r.raise_for_status()
            data=r.json()
            return [(datetime.utcfromtimestamp(int(k[6])//1000).date(), float(k[4])) for k in data]
        except Exception as e:
            last=e; continue
    raise last if last else RuntimeError("All Binance bases failed")

def pct_returns(closes): return [closes[i]/closes[i-1]-1.0 for i in range(1,len(closes))]

def zscore_series(r,look=20):
    zs=[]
    for i in range(len(r)):
        if i+1<look: zs.append(None); continue
        w=r[i+1-look:i+1]; mu=sum(w)/len(w)
        var=sum((x-mu)**2 for x in w)/len(w); sd=var**0.5
        zs.append(abs((r[i]-mu)/sd) if sd>0 else None)
    return zs

def phi(x):  # standard normal CDF without scipy
    return 0.5*(1.0+math.erf(x/math.sqrt(2.0)))

def confidence_from_z(abs_z:float)->int:
    if abs_z is None: return 0
    # Two-sided "extremeness": conf = (2*Phi(|z|)-1)*100
    c = (2*phi(abs_z)-1.0)*100.0
    return max(0, min(100, int(round(c))))

def median(vals):
    v=sorted([x for x in vals if x is not None])
    n=len(v); 
    if n==0: return None
    return v[n//2] if n%2==1 else (v[n//2-1]+v[n//2])/2.0

def median_mfe_for_coin(sym:str,state:dict)->float:
    hist=state.get("signals",{}).get(sym,[])
    mfes=[x.get("mfe") for x in hist if x.get("mfe") is not None]
    if len(mfes)>=5: return median(mfes)
    return TP_FALLBACK.get(sym,0.03)

def load_state():
    try:
        with open(STATE_FILE,"r") as f: return json.load(f)
    except Exception:
        return {"signals":{},"active_until":None,"last_run":None}

def save_state(s):
    with open(STATE_FILE,"w") as f: json.dump(s,f,default=str,indent=2)

def post_tg(text:str):
    msg=f"[{LABEL}] {text}"
    if not TG_BOT_TOKEN or not TG_CHAT_ID: print(msg); return
    try:
        requests.post(f"https://api.telegram.org/bot{TG_BOT_TOKEN}/sendMessage",
                      json={"chat_id":TG_CHAT_ID,"text":msg},timeout=20)
    except Exception as e:
        print("Telegram post error:",e,"\nMessage:\n",msg)

def fmt_price(p:float)->str:
    if p>=1000: return f"{p:,.0f}"
    if p>=100: return f"{p:,.2f}"
    if p>=1: return f"{p:,.3f}"
    return f"{p:.6f}"

def main():
    state=load_state(); state["last_run"]=datetime.now(timezone.utc).isoformat()
    active_until=state.get("active_until")
    if active_until:
        try: dt_until=datetime.fromisoformat(active_until)
        except: dt_until=None
        if dt_until and datetime.now(timezone.utc)<dt_until:
            # STATUS only while locked
            lines=[]
            for symbol,sym in COINS:
                rows=binance_daily(symbol); dates,closes=zip(*rows)
                r=pct_returns(list(closes)); zs=zscore_series(r,20)
                ret=r[-1] if r else 0.0; close=closes[-1]
                az = zs[-1] if zs else None
                conf = confidence_from_z(az) if az is not None else 0
                emoji = "🟢" if (az is not None and az>=Z_THRESH and ret>0) else ("🔴" if (az is not None and az>=Z_THRESH and ret<0) else "⚪")
                lines.append(f"{emoji} {sym} ({conf}%): close {fmt_price(close)}")
                time.sleep(0.1)
            post_tg("Status only (active window):\n"+"\n".join(lines)+f"\nActive until: {dt_until.isoformat()}")
            save_state(state); return
        else:
            state["active_until"]=None

    # Build STATUS + candidates
    lines=[]; candidates=[]
    for symbol,sym in COINS:
        try: rows=binance_daily(symbol)
        except Exception as e:
            lines.append(f"⚪ {sym} (0%): data error: {e}"); continue
        dates,closes=zip(*rows)
        r=pct_returns(list(closes)); zs=zscore_series(r,20)
        close=closes[-1]; ret=r[-1] if r else 0.0; az = zs[-1] if zs else None
        conf=confidence_from_z(az) if az is not None else 0
        emoji="🟢" if (az is not None and az>=Z_THRESH and ret>0) else ("🔴" if (az is not None and az>=Z_THRESH and ret<0) else "⚪")
        lines.append(f"{emoji} {sym} ({conf}%): close {fmt_price(close)}")
        if az is not None and az>=Z_THRESH:
            direction="SHORT" if ret>0 else "LONG"
            tp=median_mfe_for_coin(sym,state)
            entry=close; entry_date=dates[-1]
            valid_until=datetime.combine(entry_date, datetime.min.time(), tzinfo=timezone.utc)+timedelta(days=HOLD_BARS)
            candidates.append((sym,direction,entry,tp,entry_date,valid_until,conf))
        time.sleep(0.15)

    if not candidates:
        post_tg("Status:\n"+"\n".join(lines)+"\nNo trades today.")
        save_state(state); return

    priority={"BTC":0,"ETH":1,"SOL":2}
    candidates.sort(key=lambda x: priority.get(x[0],99))
    sym,direction,entry,tp,entry_date,valid_until,conf=candidates[0]

    if direction=="LONG":
        sl_price=entry*(1-SL); tp_price=entry*(1+tp)
    else:
        sl_price=entry*(1+SL); tp_price=entry*(1-tp)

    msg=("Status:\n"+ "\n".join(lines) + "\n\n"
         "Trade:\n"
         f"{sym} — {direction}  (confidence {conf}%)\n"
         f"Entry: {fmt_price(entry)}\n"
         f"SL: {fmt_price(sl_price)}  ({SL*100:.2f}%)\n"
         f"TP: {fmt_price(tp_price)}  ({tp*100:.2f}%)\n"
         f"Max hold: {HOLD_BARS*24}h\n"
         f"No overlap; next signal after: {valid_until.isoformat()}")
    post_tg(msg)

    state.setdefault("signals",{}).setdefault(sym,[]).append({
        "date":str(entry_date),"direction":direction,"entry":float(entry),
        "tp_used":float(tp),"mfe":None,"conf":conf
    })
    state["active_until"]=valid_until.isoformat()
    save_state(state)

if __name__=="__main__":
    main()
