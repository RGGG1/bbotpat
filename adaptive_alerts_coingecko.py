#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Adaptive reversal alerts (BTC/ETH/SOL) using Binance daily candles.

- Data: Binance public klines (1d), no API key needed
- Trigger: |z| >= 2.5 on 20-day return z-score (close-to-close)
- Direction: contrarian (SHORT after up day, LONG after down day)
- TP: per-coin rolling median MFE within 96h (fallback TP used until learned)
- SL: 3% (underlying)
- Hold cap: 96h (4 daily bars)
- No overlapping: skip new signals during active window
- Delivery: Telegram message (or "No trades today")

ENV VARS required:
  TG_BOT_TOKEN  - Telegram bot token from @BotFather
  TG_CHAT_ID    - your chat id (integer)

Run once daily a few minutes after daily close (e.g., 00:05 UTC).
"""

import os
import json
import time
from datetime import datetime, timezone, timedelta
from typing import List, Tuple

import requests

# ──────────────────────────────────────────────────────────────────────────────
# Config
# ──────────────────────────────────────────────────────────────────────────────
# Binance symbols + pretty tickers
COINS = [
    ("BTCUSDT", "BTC"),
    ("ETHUSDT", "ETH"),
    ("SOLUSDT", "SOL"),
]

Z_THRESH = 2.5
SL = 0.03           # 3% stop in underlying
HOLD_BARS = 4       # 96h cap
STATE_FILE = "adaptive_alerts_state.json"

# Fallback TPs (if we don't have enough learned MFEs yet)
# (from earlier analysis)
TP_FALLBACK = {"BTC": 0.0227, "ETH": 0.0167, "SOL": 0.0444}

TG_BOT_TOKEN = os.getenv("TG_BOT_TOKEN")
TG_CHAT_ID   = os.getenv("TG_CHAT_ID")


# ──────────────────────────────────────────────────────────────────────────────
# Data fetch — Binance 1d klines (public)
# ──────────────────────────────────────────────────────────────────────────────
def binance_daily(symbol: str) -> List[Tuple[datetime.date, float]]:
    """
    Returns list of (dateUTC, close) tuples for the given Binance symbol.
    """
    url = "https://api.binance.com/api/v3/klines"
    params = {"symbol": symbol, "interval": "1d", "limit": 1500}
    r = requests.get(url, params=params, timeout=30)
    r.raise_for_status()
    data = r.json()  # [ [openTime, o,h,l,c, v, closeTime, ...], ... ]
    rows = []
    for k in data:
        close_ts = int(k[6]) // 1000  # closeTime in ms → s
        close_price = float(k[4])     # close
        rows.append((datetime.utcfromtimestamp(close_ts).date(), close_price))
    return rows


# ──────────────────────────────────────────────────────────────────────────────
# Stats & signal
# ──────────────────────────────────────────────────────────────────────────────
def pct_returns(closes: List[float]) -> List[float]:
    return [closes[i]/closes[i-1]-1.0 for i in range(1, len(closes))]

def zscore_series(r: List[float], look: int = 20) -> List[float]:
    """
    Population std (pstdev) on a rolling window.
    """
    zs = []
    for i in range(len(r)):
        if i+1 < look:
            zs.append(None); continue
        window = r[i+1-look:i+1]
        mu = sum(window)/len(window)
        var = sum((x-mu)**2 for x in window)/len(window) / 1.0  # population var
        sd = var**0.5
        zs.append(abs((r[i]-mu)/sd) if sd and sd > 0 else None)
    return zs

def median(values: List[float]):
    v = sorted([x for x in values if x is not None])
    n = len(v)
    if n == 0: return None
    if n % 2 == 1: return v[n//2]
    return (v[n//2 - 1] + v[n//2]) / 2.0

def compute_mfe_path(entry_idx: int, closes: List[float], direction: str, max_bars: int = 4) -> float:
    """
    Close-to-close approximation of max favorable excursion over next max_bars bars.
    LONG: favorable is (C[j]/C[i]-1); SHORT: favorable is 1-(C[j]/C[i]).
    """
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


# ──────────────────────────────────────────────────────────────────────────────
# State & messaging
# ──────────────────────────────────────────────────────────────────────────────
def median_mfe_for_coin(sym: str, state: dict) -> float:
    """
    Return rolling median MFE learned from past signals; fallback if not enough history.
    (We’ll keep 'mfe' None until we add OHLC-based learning.)
    """
    history = state.get("signals", {}).get(sym, [])
    mfes = [x.get("mfe") for x in history if x.get("mfe") is not None]
    if len(mfes) >= 5:
        return median(mfes)
    return TP_FALLBACK.get(sym, 0.03)

def load_state() -> dict:
    try:
        with open(STATE_FILE, "r") as f:
            return json.load(f)
    except Exception:
        return {"signals": {}, "active_until": None, "last_run": None}

def save_state(state: dict) -> None:
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, default=str, indent=2)

def post_telegram(text: str):
    """
    Sends a Telegram message. If env vars are missing, prints to stdout.
    """
    if not TG_BOT_TOKEN or not TG_CHAT_ID:
        print("Telegram env vars missing; printing message:\n", text)
        return
    url = f"https://api.telegram.org/bot{TG_BOT_TOKEN}/sendMessage"
    try:
        requests.post(url, json={"chat_id": TG_CHAT_ID, "text": text}, timeout=20)
    except Exception as e:
        print("Telegram post error:", e)
        print("Message was:\n", text)


# ──────────────────────────────────────────────────────────────────────────────
# Main
# ──────────────────────────────────────────────────────────────────────────────
def main():
    state = load_state()
    state["last_run"] = datetime.now(timezone.utc).isoformat()

    # Enforce no overlapping (96h)
    active_until = state.get("active_until")
    if active_until:
        try:
            dt_until = datetime.fromisoformat(active_until)
        except Exception:
            dt_until = None
        if dt_until and datetime.now(timezone.utc) < dt_until:
            # Still inside an active window → send heartbeat and exit
            post_telegram(f"No trades today (active window until {dt_until.isoformat()}).")
            save_state(state)
            print("Heartbeat: active window; no new trades.")
            return
        else:
            state["active_until"] = None

    # Look for signals on latest close
    candidates = []
    for symbol, sym in COINS:
        try:
            data = binance_daily(symbol)
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
            # adaptive TP (rolling median MFE from prior signals; fallback until learned)
            tp = median_mfe_for_coin(sym, state)
            entry = closes[-1]
            entry_date = dates[-1]
            valid_until = datetime.combine(entry_date, datetime.min.time(), tzinfo=timezone.utc) + timedelta(days=HOLD_BARS)
            candidates.append((sym, direction, entry, tp, entry_date, valid_until))

        time.sleep(0.15)  # courtesy pacing

    if not candidates:
        post_telegram("No trades today.")
        save_state(state)
        print("No signals; heartbeat sent.")
        return

    # Portfolio-level no-overlap: pick first by fixed priority BTC > ETH > SOL
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

    # Record the shell of the signal (MFE learning can be added later with OHLC)
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
