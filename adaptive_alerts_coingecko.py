#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# (Script contents omitted here for brevity in this second cell; we'll reconstruct by reading the previous design.)

# Regenerate the same script content as in the previous cell:
import os, json, time, math, statistics, requests
from datetime import datetime, timezone, timedelta
from typing import List, Tuple

COINS = [
    ("bitcoin",  "BTC"),
    ("ethereum", "ETH"),
    ("solana",   "SOL"),
]
VS = "usd"
Z_THRESH = 2.5
SL = 0.03           # 3% stop in underlying
HOLD_BARS = 4       # 96h cap
STATE_FILE = "adaptive_alerts_state.json"

# Fallback TPs (if we don't have enough learned MFEs yet)
TP_FALLBACK = {"BTC": 0.0227, "ETH": 0.0167, "SOL": 0.0444}

TG_BOT_TOKEN = os.getenv("TG_BOT_TOKEN")
TG_CHAT_ID   = os.getenv("TG_CHAT_ID")

def cg_daily(id_str: str):
    url = "https://api.coingecko.com/api/v3/coins/{id}/market_chart"
    params = {"vs_currency": VS, "days": "max", "interval": "daily"}
    r = requests.get(url.format(id=id_str), params=params, timeout=30)
    r.raise_for_status()
    rows = r.json()["prices"]
    out = [(datetime.utcfromtimestamp(ms/1000).date(), float(px)) for ms, px in rows]
    dedup = {}
    for d, p in out:
        dedup[d] = p
    return sorted(dedup.items(), key=lambda x: x[0])

def pct_returns(closes):
    return [closes[i]/closes[i-1]-1.0 for i in range(1, len(closes))]

def zscore_series(r, look=20):
    zs = []
    for i in range(len(r)):
        if i+1 < look:
            zs.append(None); continue
        window = r[i+1-look:i+1]
        mu = sum(window)/len(window)
        # population std
        mean = mu
        var = sum((x-mean)**2 for x in window)/len(window)
        sd = var**0.5
        zs.append(abs((r[i]-mu)/sd) if sd and sd > 0 else None)
    return zs

def median(values):
    v = sorted([x for x in values if x is not None])
    n = len(v)
    if n == 0: return None
    if n % 2 == 1: return v[n//2]
    return (v[n//2 - 1] + v[n//2])/2

def compute_mfe_path(entry_idx, closes, direction, max_bars=4):
    C = closes
    i = entry_idx
    if i+1 >= len(C):
        return 0.0
    end = min(i + max_bars, len(C)-1)
    favs = []
    for j in range(i+1, end+1):
        move = (C[j]/C[i]) - 1.0
        fav = move if direction == "LONG" else -move
        favs.append(fav)
    return max(favs) if favs else 0.0

def median_mfe_for_coin(sym, state):
    history = state.get("signals", {}).get(sym, [])
    mfes = [x.get("mfe") for x in history if x.get("mfe") is not None]
    if len(mfes) >= 5:
        return median(mfes)
    return TP_FALLBACK.get(sym, 0.03)

def load_state():
    try:
        with open(STATE_FILE, "r") as f:
            return json.load(f)
    except Exception:
        return {"signals": {}, "active_until": None, "last_run": None}

def save_state(state):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, default=str, indent=2)

def post_telegram(text):
    url = f"https://api.telegram.org/bot{os.getenv('TG_BOT_TOKEN')}/sendMessage"
    chat_id = os.getenv("TG_CHAT_ID")
    if not os.getenv("TG_BOT_TOKEN") or not chat_id:
        print("Telegram env vars missing; printing message:\n", text)
        return
    try:
        requests.post(url, json={"chat_id": chat_id, "text": text}, timeout=20)
    except Exception as e:
        print("Telegram post error:", e)
        print("Message was:\n", text)

def main():
    state = load_state()
    state["last_run"] = datetime.now(timezone.utc).isoformat()

    active_until = state.get("active_until")
    if active_until:
        try:
            dt_until = datetime.fromisoformat(active_until)
        except Exception:
            dt_until = None
        if dt_until and datetime.now(timezone.utc) < dt_until:
            post_telegram(f"No trades today (active window until {dt_until.isoformat()}).")
            save_state(state)
            print("Heartbeat: active window; no new trades.")
            return
        else:
            state["active_until"] = None

    candidates = []
    for cid, sym in COINS:
        try:
            data = cg_daily(cid)
        except Exception as e:
            post_telegram(f"Data error for {sym}: {e}")
            continue
        dates, closes = zip(*data)
        if len(closes) < 25:
            continue
        r = pct_returns(list(closes))
        zs = zscore_series(r, 20)
        if not zs or zs[-1] is None:
            continue
        if zs[-1] >= Z_THRESH:
            direction = "SHORT" if r[-1] > 0 else "LONG"
            # adaptive TP (rolling median MFE from prior signals)
            tp = median_mfe_for_coin(sym, state)
            entry = closes[-1]
            entry_date = dates[-1]
            valid_until = datetime.combine(entry_date, datetime.min.time(), tzinfo=timezone.utc) + timedelta(days=HOLD_BARS)
            candidates.append((sym, direction, entry, tp, entry_date, valid_until))
        time.sleep(0.2)

    if not candidates:
        post_telegram("No trades today.")
        save_state(state)
        print("No signals; heartbeat sent.")
        return

    # fixed priority: BTC > ETH > SOL
    priority = {"BTC": 0, "ETH": 1, "SOL": 2}
    candidates.sort(key=lambda x: priority.get(x[0], 99))
    sym, direction, entry, tp, entry_date, valid_until = candidates[0]

    msg = (f"ALERT — {sym}\n"
           f"Date (UTC): {entry_date}\n"
           f"Direction: {direction} (contrarian)\n"
           f"Entry (close): {entry:.2f} USD\n"
           f"TP: {tp*100:.2f}%   SL: 3.00%   Max hold: {HOLD_BARS*24}h\n"
           f"No overlapping; next signal after: {valid_until.isoformat()}")
    post_telegram(msg)

    # record shell signal (we'll learn MFEs once we add OHLC outcome calc)
    state.setdefault("signals", {}).setdefault(sym, []).append({
        "date": str(entry_date),
        "direction": direction,
        "entry": entry,
        "tp_used": tp,
        "mfe": None
    })
    state["active_until"] = valid_until.isoformat()
    save_state(state)
    print("Alert sent:", msg)

if __name__ == "__main__":
    main()
