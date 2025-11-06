#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
44x-style daily alerts (pretty summary + sparse entries)

• Data: Binance daily klines (fully closed), 20-day z-score on daily returns
• Trigger: |z| >= 2.5 (very selective)
• Direction: contrarian (SHORT after up day, LONG after down day)
• TP: per-coin rolling median MFE (fallbacks until enough history)
• SL: 3% (informational only)
• Max hold: 96h
• One trade at a time (BTC > ETH > SOL priority)
• Telegram: header + emoji summary every day; trade block if opened
"""

import os, json, time, requests
from datetime import datetime, timezone, timedelta

# ────────────────────────────────────────────────
# Config (matches 44× backtest)
# ────────────────────────────────────────────────
COINS = [("BTCUSDT","BTC"), ("ETHUSDT","ETH"), ("SOLUSDT","SOL")]

Z_THRESH   = 2.5          # this is the real 44× trigger (abs z-score)
LOOKBACK   = 20
SL         = 0.03         # informational
HOLD_DAYS  = 4
STATE_FILE = "adaptive_alerts_state.json"

TP_FALLBACK = {"BTC":0.0227, "ETH":0.0167, "SOL":0.0444}

TG_BOT_TOKEN = os.getenv("TG_BOT_TOKEN")
TG_CHAT_ID   = os.getenv("TG_CHAT_ID")

BASES = [
    "https://data-api.binance.vision",  # mirror first (helps with 451)
    "https://api.binance.com",
    "https://api1.binance.com",
    "https://api2.binance.com",
    "https://api3.binance.com",
]
HEADERS = {"User-Agent":"alerts-44x/1.0 (+bbot)"}

# ────────────────────────────────────────────────
# Fetchers
# ────────────────────────────────────────────────
def binance_klines(symbol, interval, limit=1500, end_time_ms=None, start_time_ms=None, tries=6):
    params={"symbol":symbol,"interval":interval,"limit":limit}
    if end_time_ms is not None: params["endTime"]=end_time_ms
    if start_time_ms is not None: params["startTime"]=start_time_ms
    last_err=None; backoff=0.25
    for _ in range(tries):
        for base in BASES:
            try:
                r=requests.get(f"{base}/api/v3/klines", params=params, headers=HEADERS, timeout=30)
                if r.status_code in (451,403):  # blocked
                    last_err = requests.HTTPError(f"{r.status_code} {r.reason}"); continue
                r.raise_for_status()
                return r.json()
            except Exception as e:
                last_err=e; time.sleep(backoff)
        backoff=min(2.0, backoff*1.8)
    raise last_err if last_err else RuntimeError("All Binance bases failed")

def binance_daily(symbol):
    """Fully closed daily candles up to yesterday 23:59:59.999 UTC."""
    now = datetime.utcnow().replace(tzinfo=timezone.utc)
    y   = now - timedelta(days=1)
    end_ms = int(datetime(y.year,y.month,y.day,23,59,59,tzinfo=timezone.utc).timestamp()*1000) + 999
    ks = binance_klines(symbol,"1d",1500,end_time_ms=end_ms)
    rows=[]
    for k in ks:
        ct=int(k[6])//1000
        rows.append((datetime.utcfromtimestamp(ct).date(), float(k[4])))
    return rows

# ────────────────────────────────────────────────
# Stats helpers
# ────────────────────────────────────────────────
def pct_returns(closes):
    return [closes[i]/closes[i-1]-1.0 for i in range(1,len(closes))]

def z_and_level_for_today(closes, look=20):
    """
    Returns (z_signed, z_abs, level%), where level% is our 'confidence' display:
      level = clamp(0..100, round(50 + 20 * z_signed))
    Note: z_abs >= 2.5 is the true trigger for this strategy.
    """
    r = pct_returns(closes)
    if len(r) < look: return None, None, None
    window = r[-look:]                 # last `look` returns ending at today
    mu = sum(window)/look
    var = sum((x-mu)**2 for x in window)/look
    sd  = var**0.5 if var>0 else 0.0
    if sd <= 0: return None, None, None
    z_signed = (r[-1]-mu)/sd
    z_abs    = abs(z_signed)
    level    = max(0, min(100, round(50 + 20*z_signed)))
    return z_signed, z_abs, level

def median(vals):
    v = sorted([x for x in vals if x is not None])
    if not v: return None
    n=len(v)
    return v[n//2] if n%2 else (v[n//2-1]+v[n//2])/2

# ────────────────────────────────────────────────
# State & Telegram
# ────────────────────────────────────────────────
def load_state():
    try:
        with open(STATE_FILE,"r") as f: return json.load(f)
    except Exception:
        return {"signals":{}, "active_until":None, "last_run":None}

def save_state(state):
    with open(STATE_FILE,"w") as f: json.dump(state,f,default=str,indent=2)

def post_tg(text):
    if not TG_BOT_TOKEN or not TG_CHAT_ID:
        print("[TG] Missing token/chat. Message:\n", text); return
    try:
        requests.post(f"https://api.telegram.org/bot{TG_BOT_TOKEN}/sendMessage",
                      json={"chat_id":TG_CHAT_ID,"text":text}, timeout=20)
    except Exception as e:
        print("Telegram post error:", e, "\nMessage:\n", text)

def fmt_price(x):
    if x >= 1000: return f"{x:,.2f}"
    if x >= 100:  return f"{x:,.2f}"
    if x >= 1:    return f"{x:,.2f}"
    return f"{x:,.4f}"

# ────────────────────────────────────────────────
# Main
# ────────────────────────────────────────────────
def main():
    state=load_state()
    state["last_run"]=datetime.now(timezone.utc).isoformat()

    # 1) build summary (emoji + status + close + confidence%)
    summary_lines=[]
    latest=[]
    for symbol,sym in COINS:
        try:
            data=binance_daily(symbol)
        except Exception as e:
            summary_lines.append(f"{sym}: ⚠️ data error ({e})"); continue
        dates, closes = zip(*data)
        z_signed, z_abs, level = z_and_level_for_today(list(closes), LOOKBACK)
        if z_signed is None:
            summary_lines.append(f"{sym}: ⚪ No data"); continue

        direction = "SHORT" if z_signed>0 else "LONG"
        close_px  = closes[-1]

        # pretty label
        if z_abs >= Z_THRESH:
            # strong band → red/green with threshold note
            if direction=="SHORT":
                emoji="🔴"; desc=f"Overbought {level}% (|z|≥{Z_THRESH:.1f})"
            else:
                emoji="🟢"; desc=f"Oversold {level}% (|z|≥{Z_THRESH:.1f})"
        else:
            # neutral band
            emoji="⚪"
            if z_signed>0:  desc=f"Warm {level}%"
            elif z_signed<0:desc=f"Cool {level}%"
            else:           desc=f"Neutral {level}%"

        summary_lines.append(f"{emoji} {sym}: {desc} | Close: ${fmt_price(close_px)}")
        latest.append((sym, dates[-1], close_px, z_signed, z_abs, level))
        time.sleep(0.1)

    today=datetime.utcnow().strftime("%b %d, %Y")
    header=f"📊 Daily Crypto Report — {today}"
    summary="\n".join(summary_lines)

    # 2) no-overlap window?
    active_until_iso = state.get("active_until")
    if active_until_iso:
        try:
            active_until = datetime.fromisoformat(active_until_iso)
        except Exception:
            active_until = None
        if active_until and datetime.now(timezone.utc) < active_until:
            # still in window → send pretty heartbeat (not the bare line)
            post_tg(f"{header}\n\n{summary}\n\nNo new trade (active until {active_until_iso}).")
            save_state(state); print("Heartbeat sent."); return
        else:
            state["active_until"]=None

    # 3) find candidates today, pick BTC>ETH>SOL, |z|>=2.5 (true 44×)
    candidates=[]
    for (sym, date, close_px, z_signed, z_abs, level) in latest:
        if z_abs >= Z_THRESH:
            direction = "SHORT" if z_signed>0 else "LONG"
            # adaptive TP from prior MFEs (if we have them)
            hist = state.get("signals",{}).get(sym,[])
            mfes = [x.get("mfe") for x in hist if x.get("mfe") is not None]
            tp = median(mfes)/100.0 if len(mfes)>=5 else TP_FALLBACK.get(sym,0.03)
            valid_until = datetime.combine(date, datetime.min.time(), tzinfo=timezone.utc) + timedelta(days=HOLD_DAYS)
            candidates.append((sym, direction, close_px, tp, date, valid_until))

    if not candidates:
        post_tg(f"{header}\n\n{summary}\n\nNo trades today.")
        save_state(state); print("No trade; summary sent."); return

    priority={"BTC":0,"ETH":1,"SOL":2}
    candidates.sort(key=lambda x:priority.get(x[0],99))
    sym, direction, entry, tp, entry_date, valid_until = candidates[0]

    # 4) send trade block after summary
    block = [
        f"🚨 TRADE SIGNAL: {sym}",
        f"Side: {direction}",
        f"Anchor (prev daily close): ${fmt_price(entry)}",
        f"TP: {tp*100:.2f}%   SL: {SL*100:.2f}%",
        f"Max hold: {HOLD_DAYS*24}h",
        f"Valid until: {valid_until.isoformat()}",
    ]
    post_tg(f"{header}\n\n{summary}\n\n" + "\n".join(block))

    # record light signal (we keep mfe=None until a separate learner fills it)
    state.setdefault("signals",{}).setdefault(sym,[]).append({
        "date": str(entry_date), "direction": direction,
        "entry": entry, "tp_used": tp, "mfe": None
    })
    state["active_until"] = valid_until.isoformat()
    save_state(state)
    print("Alert sent.")
    
if __name__=="__main__":
    main()
  
