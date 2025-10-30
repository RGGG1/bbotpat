#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
alerts_binance.py
v2.5 – Daily adaptive signals + dashboard + pyramiding + exit advisory
     – Uses Binance endTime to fetch ONLY fully closed daily candles.

Adds vs v2.4:
- Candle fetch now sets endTime = today's UTC midnight - 1 ms,
  ensuring we never read the still-forming daily candle.
"""

import os, json, time, requests
from datetime import datetime, timezone, timedelta

# ────────────────────────────────────────────────
# Config
# ────────────────────────────────────────────────
COINS = [("BTCUSDT","BTC"), ("ETHUSDT","ETH"), ("SOLUSDT","SOL")]

CONF_TRIGGER = 77            # heat threshold for short; long uses 100-CONF_TRIGGER (=23)
SL = 0.03                    # shown in messages (you execute manually)
HOLD_BARS = 4                # 96h
STATE_FILE = "adaptive_alerts_state.json"

TP_FALLBACK = {"BTC":0.0227, "ETH":0.0167, "SOL":0.0444}

# Pyramiding
BASE_LEV = 10
MAX_LEV  = 14
CONF_PER_LEV = 5            # +1x per +5% confidence gain (integer steps)

# Telegram
TG_BOT_TOKEN = os.getenv("TG_BOT_TOKEN")
TG_CHAT_ID   = os.getenv("TG_CHAT_ID")

# Binance endpoints (with mirrors)
BASES = [
    "https://api.binance.com",
    "https://api1.binance.com",
    "https://api2.binance.com",
    "https://api3.binance.com",
    "https://data-api.binance.vision",
]
HEADERS = {"User-Agent": "crypto-alert-bot/2.5 (+github actions)"}

# ────────────────────────────────────────────────
# Data helpers
# ────────────────────────────────────────────────
def binance_daily(symbol):
    """
    Download fully closed DAILY candles with fallback endpoints.
    We set endTime to today's midnight UTC minus 1 ms, so Binance returns
    only completed candles (excluding the still-forming candle).
    """
    last_err = None
    for base in BASES:
        try:
            url = f"{base}/api/v3/klines"

            # endTime = today's UTC midnight - 1 ms
            utc_now = datetime.utcnow()
            utc_midnight = datetime(utc_now.year, utc_now.month, utc_now.day, tzinfo=timezone.utc)
            end_time_ms = int(utc_midnight.timestamp() * 1000) - 1

            params = {
                "symbol": symbol,
                "interval": "1d",
                "limit": 1500,
                "endTime": end_time_ms
            }

            r = requests.get(url, params=params, headers=HEADERS, timeout=30)
            r.raise_for_status()
            data = r.json()

            rows = []
            for k in data:
                close_ts = int(k[6]) // 1000  # k[6] = close time (ms)
                close_price = float(k[4])     # k[4] = close price
                rows.append((datetime.utcfromtimestamp(close_ts).date(), close_price))
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
        if i+1 < look:
            zs.append(None); continue
        w=r[i+1-look:i+1]
        mu=sum(w)/look
        sd=(sum((x-mu)**2 for x in w)/look)**0.5
        zs.append(abs((r[i]-mu)/sd) if sd>0 else None)
    return zs

def median(v):
    v=[x for x in v if x is not None]; v.sort()
    n=len(v)
    if n==0: return None
    if n%2: return v[n//2]
    return (v[n//2-1]+v[n//2])/2

def median_mfe_for_coin(sym, state):
    hist=state.get("signals",{}).get(sym,[])
    mfes=[x.get("mfe") for x in hist if x.get("mfe") is not None]
    return median(mfes) if len(mfes)>=5 else TP_FALLBACK.get(sym,0.03)

# ────────────────────────────────────────────────
# State & Telegram
# ────────────────────────────────────────────────
def load_state():
    try:
        with open(STATE_FILE,"r") as f: return json.load(f)
    except Exception:
        return {"signals":{}, "active_trade":None, "last_run":None}

def save_state(state):
    with open(STATE_FILE,"w") as f: json.dump(state,f,default=str,indent=2)

def post_telegram(text):
    if not TG_BOT_TOKEN or not TG_CHAT_ID:
        print("⚠️ Missing Telegram vars. Message:\n", text); return
    try:
        requests.post(f"https://api.telegram.org/bot{TG_BOT_TOKEN}/sendMessage",
                      json={"chat_id":TG_CHAT_ID,"text":text}, timeout=20)
    except Exception as e:
        print("Telegram post error:", e, "\nMessage:\n", text)

# ────────────────────────────────────────────────
# Main
# ────────────────────────────────────────────────
def main():
    state=load_state()
    state["last_run"]=datetime.now(timezone.utc).isoformat()

    active=state.get("active_trade")  # None or dict

    # Evaluate each coin
    summary_lines=[]
    latest_by_sym={}  # sym -> dict(level, direction, price, date)

    for symbol,sym in COINS:
        try:
            data=binance_daily(symbol)
        except Exception as e:
            summary_lines.append(f"{sym}: ⚠️ Data error ({e})"); continue

        dates,closes=zip(*data)
        if len(closes)<25:
            summary_lines.append(f"{sym}: Insufficient data"); continue

        r=pct_returns(list(closes))
        zs=zscore_series(r,20)
        if not zs or zs[-1] is None:
            summary_lines.append(f"{sym}: No recent data"); continue

        z=zs[-1]; ret=r[-1]
        z_signed = z if ret>0 else -z
        level = max(0, min(100, round(50 + z_signed*20)))

        if level>=CONF_TRIGGER:
            emoji="🔴"; desc=f"Overbought {level}% (needs ≥{CONF_TRIGGER}% to short)"
        elif level<=100-CONF_TRIGGER:
            emoji="🟢"; desc=f"Oversold {level}% (needs ≤{100-CONF_TRIGGER}% to long)"
        else:
            emoji="⚪"; desc=f"Neutral {level}%"
        summary_lines.append(f"{emoji} {sym}: {desc}")

        direction="SHORT" if ret>0 else "LONG"
        latest_by_sym[sym]={"level":level,"direction":direction,"price":closes[-1],"date":dates[-1]}

        time.sleep(0.12)

    today=datetime.utcnow().strftime("%b %d, %Y")
    header=f"📊 Daily Crypto Report — {today}"
    summary="\n".join(summary_lines)

    now_utc=datetime.now(timezone.utc)
    def parse_iso(ts):
        try: return datetime.fromisoformat(ts)
        except Exception: return None

    msg_tail=""
    opened_new=False

    # ── Active trade management: reinforcement & exit advisory ──
    if active:
        valid_until_dt=parse_iso(active.get("valid_until")) if active.get("valid_until") else None
        still_active = valid_until_dt and now_utc < valid_until_dt

        sym=active["sym"]
        prev_conf=float(active.get("confidence", CONF_TRIGGER))
        active_dir=active["direction"]
        entry=float(active["entry"])
        cur=latest_by_sym.get(sym)

        if still_active and cur:
            # Current signed move since entry (close-to-close)
            if active_dir=="LONG":
                cur_move = cur["price"]/entry - 1.0
                trigger_band = cur["level"] <= 100-CONF_TRIGGER
            else:
                cur_move = entry/cur["price"] - 1.0
                trigger_band = cur["level"] >= CONF_TRIGGER

            # 1) Pyramiding if confidence increased (same direction + stronger)
            same_direction = (cur["direction"] == active_dir)
            if same_direction and cur["level"] > prev_conf and trigger_band:
                delta_conf = cur["level"] - prev_conf
                add_units  = int(delta_conf // CONF_PER_LEV)
                if add_units > 0:
                    new_lev=min(int(active.get("leverage",BASE_LEV))+add_units, MAX_LEV)
                    if new_lev > active.get("leverage", BASE_LEV):
                        active["leverage"]=new_lev
                        active["confidence"]=cur["level"]
                        msg_tail += (f"\n📈 Reinforcing signal on {sym}: confidence {prev_conf:.0f}% → {cur['level']:.0f}% "
                                     f"(+{delta_conf:.0f}%). Leverage increased to {new_lev}× (cap {MAX_LEV}×).")
                else:
                    msg_tail += (f"\nℹ️ {sym} confidence up to {cur['level']:.0f}% (no +1× step; {CONF_PER_LEV}% per +1×).")

            # 2) EXIT ADVISORY: confidence cooled AND new TP lower AND current move >= new TP
            new_tp = median_mfe_for_coin(sym, state)
            lower_conf = cur["level"] < prev_conf
            lower_tp   = new_tp < float(active.get("tp_used", new_tp))
            ahead_of_new_tp = cur_move >= new_tp - 1e-6  # tiny epsilon

            if same_direction and lower_conf and lower_tp and ahead_of_new_tp and trigger_band:
                msg_tail += (f"\n🎯 Exit advisory ({sym}): confidence cooled ({prev_conf:.0f}% → {cur['level']:.0f}%), "
                             f"new TP {new_tp*100:.2f}% < current TP {active.get('tp_used', new_tp)*100:.2f}%. "
                             f"Current move ≈ {cur_move*100:.2f}% ≥ new TP → consider taking profit early.")

            # Keep active trade
            state["active_trade"]=active
        else:
            # Window ended; free the slot (execution remains manual)
            state["active_trade"]=None

    # ── Entry: if no active trade, open highest priority candidate ──
    if not state.get("active_trade"):
        candidates=[]
        for sym in ["BTC","ETH","SOL"]:
            snap=latest_by_sym.get(sym)
            if not snap: continue
            lvl=snap["level"]; dirn=snap["direction"]; px=snap["price"]; d=snap["date"]
            if (lvl>=CONF_TRIGGER) or (lvl<=100-CONF_TRIGGER):
                tp=median_mfe_for_coin(sym,state)
                valid_until=datetime.combine(d, datetime.min.time(), tzinfo=timezone.utc) + timedelta(days=HOLD_BARS)
                candidates.append((sym,dirn,px,tp,d,valid_until,lvl))
        if candidates:
            priority={"BTC":0,"ETH":1,"SOL":2}
            candidates.sort(key=lambda x:priority.get(x[0],99))
            sym,dirn,entry,tp,edate,until,lvl=candidates[0]
            opened_new=True
            state["active_trade"]={
                "sym":sym,"direction":dirn,"entry":entry,"tp_used":tp,
                "leverage":BASE_LEV,"confidence":float(lvl),
                "start_date":str(edate),"valid_until":until.isoformat()
            }
            state.setdefault("signals",{}).setdefault(sym,[]).append({
                "date":str(edate),"direction":dirn,"entry":entry,"tp_used":tp,"mfe":None
            })

    # ── Compose Telegram message ──
    today=datetime.utcnow().strftime("%b %d, %Y")
    header=f"📊 Daily Crypto Report — {today}"
    summary="\n".join(summary_lines)

    if opened_new:
        a=state["active_trade"]
        msg=(f"{header}\n\n{summary}\n\n"
             f"✅ *Active Trade: {a['sym']}*\n"
             f"Direction: {a['direction']}\n"
             f"Entry: {a['entry']:.2f} USD\n"
             f"TP: {a['tp_used']*100:.2f}% | SL: 3.00%\n"
             f"Leverage: {a['leverage']}× (base)\n"
             f"Hold: {HOLD_BARS*24} h\n"
             f"Valid until: {a['valid_until']}")
    elif state.get("active_trade"):
        a=state["active_trade"]
        msg=(f"{header}\n\n{summary}\n\n"
             f"🟨 Active trade unchanged: {a['sym']} ({a['direction']})\n"
             f"Entry: {a['entry']:.2f} USD | TP: {a['tp_used']*100:.2f}% | SL: 3.00%\n"
             f"Leverage: {a['leverage']}× | Valid until: {a['valid_until']}"
             f"{msg_tail}")
    else:
        msg=f"{header}\n\n{summary}\n\nNo trades today."

    post_telegram(msg)
    save_state(state)
    print(msg)

# ────────────────────────────────────────────────
if __name__=="__main__":
    main()
  
