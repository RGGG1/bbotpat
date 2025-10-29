#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
alerts_binance.py
v2.1 – Binance adaptive signals + Telegram dashboard
Trigger level set to 77 % for all coins.
"""

import os, json, time, requests
from datetime import datetime, timezone, timedelta

COINS = [("BTCUSDT", "BTC"), ("ETHUSDT", "ETH"), ("SOLUSDT", "SOL")]
Z_THRESH = 2.5                 # z-score trigger
CONF_TRIGGER = 77              # % heat level required to signal
SL = 0.03
HOLD_BARS = 4                  # 96 h
STATE_FILE = "adaptive_alerts_state.json"
TP_FALLBACK = {"BTC": 0.0227, "ETH": 0.0167, "SOL": 0.0444}

TG_BOT_TOKEN = os.getenv("TG_BOT_TOKEN")
TG_CHAT_ID   = os.getenv("TG_CHAT_ID")

BASES = [
    "https://api.binance.com",
    "https://api1.binance.com",
    "https://api2.binance.com",
    "https://api3.binance.com",
    "https://data-api.binance.vision"
]
HEADERS = {"User-Agent": "crypto-alert-bot/2.1 (+github actions)"}

# ────────────────────────────────────────────────
def binance_daily(symbol):
    last_err = None
    for base in BASES:
        try:
            url = f"{base}/api/v3/klines"
            r = requests.get(url,
                params={"symbol": symbol, "interval": "1d", "limit": 1500},
                headers=HEADERS, timeout=30)
            r.raise_for_status()
            data = r.json()
            rows = []
            for k in data:
                close_ts = int(k[6]) // 1000
                rows.append((datetime.utcfromtimestamp(close_ts).date(),
                             float(k[4])))
            return rows
        except Exception as e:
            last_err = e
            continue
    raise last_err if last_err else RuntimeError("All Binance bases failed")

def pct_returns(closes):
    return [closes[i]/closes[i-1]-1 for i in range(1,len(closes))]

def zscore_series(r, look=20):
    zs=[]
    for i in range(len(r)):
        if i+1<look:
            zs.append(None); continue
        w=r[i+1-look:i+1]; mu=sum(w)/look
        sd=(sum((x-mu)**2 for x in w)/look)**0.5
        zs.append(abs((r[i]-mu)/sd) if sd>0 else None)
    return zs

def median(v):
    v=[x for x in v if x is not None]; v.sort()
    n=len(v)
    if n==0: return None
    if n%2: return v[n//2]
    return (v[n//2-1]+v[n//2])/2

def median_mfe_for_coin(sym,state):
    hist=state.get("signals",{}).get(sym,[])
    mfes=[x.get("mfe") for x in hist if x.get("mfe") is not None]
    return median(mfes) if len(mfes)>=5 else TP_FALLBACK.get(sym,0.03)

def load_state():
    try:
        with open(STATE_FILE,"r") as f: return json.load(f)
    except Exception: return {"signals":{}, "active_until":None, "last_run":None}

def save_state(state):
    with open(STATE_FILE,"w") as f: json.dump(state,f,default=str,indent=2)

def post_telegram(text):
    if not TG_BOT_TOKEN or not TG_CHAT_ID:
        print("⚠️ Missing Telegram vars. Message:\n",text); return
    try:
        requests.post(f"https://api.telegram.org/bot{TG_BOT_TOKEN}/sendMessage",
                      json={"chat_id":TG_CHAT_ID,"text":text},timeout=20)
    except Exception as e:
        print("Telegram post error:",e); print("Message:\n",text)

# ────────────────────────────────────────────────
def main():
    state=load_state()
    state["last_run"]=datetime.now(timezone.utc).isoformat()

    active_until=state.get("active_until")
    if active_until:
        try: dt_until=datetime.fromisoformat(active_until)
        except Exception: dt_until=None
        if dt_until and datetime.now(timezone.utc)<dt_until:
            post_telegram(f"No trades today (active window until {dt_until.isoformat()}).")
            save_state(state); return
        else: state["active_until"]=None

    summary=[]; candidates=[]

    for symbol,sym in COINS:
        try: data=binance_daily(symbol)
        except Exception as e:
            summary.append(f"{sym}: ⚠️ Data error ({e})"); continue
        dates,closes=zip(*data)
        if len(closes)<25:
            summary.append(f"{sym}: Insufficient data"); continue
        r=pct_returns(list(closes)); zs=zscore_series(r,20)
        if not zs or zs[-1] is None:
            summary.append(f"{sym}: No recent data"); continue

        z=zs[-1]; ret=r[-1]
        z_signed = z if ret>0 else -z
        level = max(0,min(100,round(50+z_signed*20)))

        # display line
        if level>=CONF_TRIGGER:
            emoji="🔴"; desc=f"Overbought {level}% (needs ≥{CONF_TRIGGER}% to short)"
        elif level<=100-CONF_TRIGGER:
            emoji="🟢"; desc=f"Oversold {level}% (needs ≤{100-CONF_TRIGGER}% to long)"
        else:
            emoji="⚪"; desc=f"Neutral {level}%"
        summary.append(f"{emoji} {sym}: {desc}")

        # trigger condition
        if level>=CONF_TRIGGER or level<=100-CONF_TRIGGER:
            direction="SHORT" if ret>0 else "LONG"
            tp=median_mfe_for_coin(sym,state)
            entry=closes[-1]; entry_date=dates[-1]
            valid_until=datetime.combine(entry_date,datetime.min.time(),
                                         tzinfo=timezone.utc)+timedelta(days=HOLD_BARS)
            candidates.append((sym,direction,entry,tp,entry_date,valid_until))

        time.sleep(0.15)

    today=datetime.utcnow().strftime("%b %d, %Y")
    header=f"📊 Daily Crypto Report — {today}"
    body="\n".join(summary)

    if not candidates:
        msg=f"{header}\n\n{body}\n\nNo trades today."
    else:
        priority={"BTC":0,"ETH":1,"SOL":2}
        candidates.sort(key=lambda x:priority.get(x[0],99))
        sym,dirn,entry,tp,edate,until=candidates[0]
        msg=(f"{header}\n\n{body}\n\n"
             f"✅ *Active Trade: {sym}*\n"
             f"Direction: {dirn}\nEntry: {entry:.2f} USD\n"
             f"TP: {tp*100:.2f}% | SL: 3.00%\n"
             f"Hold: {HOLD_BARS*24} h\nValid until: {until.isoformat()}")
        state.setdefault("signals",{}).setdefault(sym,[]).append({
            "date":str(edate),"direction":dirn,"entry":entry,"tp_used":tp,"mfe":None})
        state["active_until"]=until.isoformat()

    post_telegram(msg)
    save_state(state)
    print(msg)

# ────────────────────────────────────────────────
if __name__=="__main__":
    main()
    
