#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
v1.4 — Extreme-move algo, closed-only candles, status board + confidence%

- Interval: 1d
- Signals from CLOSED daily candles only
- Thresholds: |Δ| >= coin threshold (%)
- TP: coin-specific medians (ETH≈4.34%, SOL≈7.87%; fallback 6.5%)
- SL: 5% underlying
- No-overlap 96h

ENV:
  TG_BOT_TOKEN, TG_CHAT_ID, ALERTS_NAME (optional)
"""

import os, json, time
from datetime import datetime, timezone, timedelta
from typing import List, Tuple
import requests

# ── Config ────────────────────────────────────────────────────────────────────
COINS=[("BTCUSDT","BTC"),("ETHUSDT","ETH"),("SOLUSDT","SOL"),("BNBUSDT","BNB"),("XRPUSDT","XRP")]
THRESHOLDS_PCT={"BTC":13,"ETH":14,"SOL":17,"BNB":20,"XRP":15}
COIN_TP={"ETH":0.0434,"SOL":0.0787}
TP_FALLBACK=0.065
SL=0.05
HOLD_BARS=4
STATE_FILE="alerts10_extreme_state.json"

TG_BOT_TOKEN=os.getenv("TG_BOT_TOKEN")
TG_CHAT_ID=os.getenv("TG_CHAT_ID")
LABEL=os.getenv("ALERTS_NAME","ALGO10")

BASES=[
 "https://api.binance.com","https://api1.binance.com","https://api2.binance.com",
 "https://api3.binance.com","https://data-api.binance.vision"
]
HEADERS={"User-Agent":"alerts10/1.4 (+https://github.com)"}

# ── Binance helpers ───────────────────────────────────────────────────────────
def fetch_klines_daily(symbol:str):
    last=None
    for base in BASES:
        try:
            r=requests.get(f"{base}/api/v3/klines",
                           params={"symbol":symbol,"interval":"1d","limit":1500},
                           headers=HEADERS,timeout=30)
            r.raise_for_status()
            return r.json()
        except Exception as e:
            last=e; continue
    raise last if last else RuntimeError("All Binance bases failed")

def parse_klines(data):
    rows=[]
    for k in data:
        rows.append({
            "open_time": int(k[0]),
            "close_time": int(k[6]),
            "close": float(k[4]),
        })
    return rows

def all_closed_closes(rows, n=None):
    now_ms = int(datetime.now(timezone.utc).timestamp()*1000)
    closed = [r["close"] for r in rows if r["close_time"] <= now_ms]
    return closed if n is None else closed[-n:]

# ── Utils ─────────────────────────────────────────────────────────────────────
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
    if p>=100:  return f"{p:,.2f}"
    if p>=1:    return f"{p:,.3f}"
    return f"{p:.6f}"

def confidence_from_move(abs_move_pct:float, thresh_pct:float)->int:
    """
    Map absolute move vs threshold to 0..100:
      below T: 0..50 linearly; T..2T: 50..100 linearly; cap at 100.
    """
    if thresh_pct<=0: return 0
    r=abs_move_pct/thresh_pct
    if r<1.0: conf=r*50.0
    else:     conf=50.0 + min(1.0, r-1.0)*50.0
    return int(round(max(0.0,min(100.0,conf))))

# ── Main ──────────────────────────────────────────────────────────────────────
def main():
    # Load state for overlap window
    try:
        with open(STATE_FILE,"r") as f: state=json.load(f)
    except Exception:
        state={"active_until":None,"last_run":None}
    state["last_run"]=datetime.now(timezone.utc).isoformat()

    active_until=state.get("active_until")
    if active_until:
        try: dt_until=datetime.fromisoformat(active_until)
        except: dt_until=None
        if dt_until and datetime.now(timezone.utc)<dt_until:
            # STATUS only
            lines=[]
            for symbol,sym in COINS:
                data=fetch_klines_daily(symbol)
                rows=parse_klines(data)
                closes=all_closed_closes(rows)
                if len(closes)<2:
                    lines.append(f"⚪ {sym} (0%): close n/a"); continue
                p0,p1=closes[-2],closes[-1]
                dayret=(p1/p0-1.0)*100.0
                T=THRESHOLDS_PCT[sym]
                conf=confidence_from_move(abs(dayret),T)
                emoji="🟢" if dayret>= T else ("🔴" if dayret<= -T else "⚪")
                lines.append(f"{emoji} {sym} ({conf}%): close {fmt_price(p1)}")
                time.sleep(0.1)
            post_tg("Status only (active window):\n"+"\n".join(lines)+f"\nActive until: {dt_until.isoformat()}")
            with open(STATE_FILE,"w") as f: json.dump(state,f,default=str,indent=2)
            return
        else:
            state["active_until"]=None

    # Status + candidates
    lines=[]; candidates=[]
    for symbol,sym in COINS:
        try:
            data=fetch_klines_daily(symbol)
            rows=parse_klines(data)
            closes=all_closed_closes(rows)
        except Exception as e:
            lines.append(f"⚪ {sym} (0%): data error: {e}")
            continue

        if len(closes)<2:
            lines.append(f"⚪ {sym} (0%): close n/a"); continue

        p0,p1=closes[-2],closes[-1]
        dayret=(p1/p0-1.0)*100.0
        T=THRESHOLDS_PCT[sym]
        conf=confidence_from_move(abs(dayret),T)
        emoji="🟢" if dayret>= T else ("🔴" if dayret<= -T else "⚪")
        lines.append(f"{emoji} {sym} ({conf}%): close {fmt_price(p1)}")

        if abs(dayret)>=T:
            direction="SHORT" if dayret>0 else "LONG"
            tp=COIN_TP.get(sym,TP_FALLBACK)
            entry=p1
            entry_date = datetime.utcfromtimestamp(rows[-1]["close_time"]/1000).date()
            valid_until = datetime.combine(entry_date, datetime.min.time(), tzinfo=timezone.utc)+timedelta(days=HOLD_BARS)
            candidates.append((sym,direction,entry,tp,entry_date,valid_until,conf))

        time.sleep(0.1)

    if not candidates:
        post_tg("Status:\n"+"\n".join(lines)+"\nNo trades today.")
        with open(STATE_FILE,"w") as f: json.dump(state,f,default=str,indent=2)
        return

    priority={"BTC":0,"ETH":1,"SOL":2,"BNB":3,"XRP":4}
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

    state["active_until"]=valid_until.isoformat()
    with open(STATE_FILE,"w") as f: json.dump(state,f,default=str,indent=2)

if __name__=="__main__":
    main()
