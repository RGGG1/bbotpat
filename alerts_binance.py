#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
v1.1 (Binance with multi-endpoint + mirror fallback)
Adaptive reversal alerts (BTC/ETH/SOL) using Binance daily candles.

- Data: Binance public klines (1d), with fallback to api1/api2/api3 + data-api.binance.vision
- Trigger: |z| >= 2.5 on 20-day return z-score (close-to-close)
- Direction: contrarian (SHORT after up day, LONG after down day)
- TP: per-coin rolling median MFE within 96h (fallback TP used until learned)
- SL: 3% (underlying)
- Hold cap: 96h (4 daily bars)
- No overlapping: skip new signals during active window
- Delivery: Telegram message (or "No trades today")

ENV VARS:
  TG_BOT_TOKEN  - Telegram bot token from @BotFather
  TG_CHAT_ID    - your chat id (integer)

Schedule to run once daily a few minutes after daily close (e.g., 00:05 UTC).
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
COINS = [
    ("BTCUSDT", "BTC"),
    ("ETHUSDT", "ETH"),
    ("SOLUSDT", "SOL"),
]

Z_THRESH = 2.5
SL = 0.03            # 3% stop in underlying
HOLD_BARS = 4        # 96h cap
STATE_FILE = "adaptive_alerts_state.json"

# Fallback TPs (until we learn MFEs per coin)
TP_FALLBACK = {"BTC": 0.0227, "ETH": 0.0167, "SOL": 0.0444}

TG_BOT_TOKEN = os.getenv("TG_BOT_TOKEN")
TG_CHAT_ID   = os.getenv("TG_CHAT_ID")

# Binance bases (try in order), ending with the public mirror
BASES = [
    "https://api.binance.com",
    "https://api1.binance.com",
    "https://api2.binance.com",
    "https://api3.binance.com",
    "https://data-api.binance.vision",  # public mirror
]

HEADERS = {
    "User-Agent": "alerts-bot/1.1 (+https://github.com)"
}

# ──────────────────────────────────────────────────────────────────────────────
# Data fetch — Binance 1d klines with multi-endpoint fallback
# ──────────────────────────────────────────────────────────────────────────────
def binance_daily(symbol: str) -> List[Tuple[datetime.date, float]]:
    """
    Returns list of (dateUTC, close) tuples for the given Binance symbol.
    Tries multiple base URLs to avoid geo/policy blocks (HTTP 451) and similar issues.
    """
    last_err = None
    for base in BASES:
        try:
            url = f"{base}/api/v3/klines"
            params = {"symbol": symbol, "interval": "1d", "limit": 1500}
            r = requests.get(url, params=params, headers=HEADERS, timeout=30)
            r.raise_for_status()
            data = r.json()  # [ [openTime, o,h,l,c, v, closeTime, ...], ... ]
            rows = []
            for k in data:
                close_ts = int(k[6]) // 1000  # closeTime (ms) -> s
                close_price = float(k[4])     # close
                rows.append((datetime.utcfromtimestamp(close_ts).date(), close_price))
            return rows  # success on this base
        except requests.HTTPError as e:
            last_err = e
            code = e.response.status_code if e.response is not None else None
            # Try next base on typical network/geo/rate errors
            if code in (451, 403, 429, 520, 521, 522, 523, 524):
                continue
            # Other HTTP errors: propagate immediately
            raise
        except Exception as e:
            last_err = e
            # Transient/connect errors → try next base
            continue
    # All bases failed
    raise last_err if last_err else RuntimeError("All Binance bases failed")

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
        var = sum((x-mu)**2 for x in window)/len(window)
        sd = var**0.5
        zs.append(abs((r[i]-mu)/sd) if sd and sd > 0 else None)
    return zs

def median(values):
    v = sorted([x for x in values if x is not None])
    n = len(v)
    if n == 0: return None
    if n % 2 == 1: return v[n//2]
    return (v[n//2 - 1] + v[n//2]) / 2.0

def median_mfe_for_coin(sym: str, state: dict) -> float:
    """
    Return rolling median MFE learned from past signals; fallback if not enough history.
    (We'll keep 'mfe' None until we add OHLC-based learning.)
    """
    hist = state.get("signals", {}).get(sym, [])
    mfes = [x.get("mfe") for x in hist if x.get("mfe") is not None]
    if len(mfes) >= 5:
        return median(mfes)
    return TP_FALLBACK.get(sym, 0.03)

# ──────────────────────────────────────────────────────────────────────────────
# State & messaging
# ──────────────────────────────────────────────────────────────────────────────
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
            # Still in an active window → heartbeat and exit
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
            tp = median_mfe_for_coin(sym, state)
            entry = closes[-1]
            entry_date = dates[-1]
            valid_until = datetime.combine(entry_date, datetime.min.time(), tzinfo=timezone.utc) + timedelta(days=HOLD_BARS)
            candidates.append((sym, direction, entry, tp, entry_date, valid_until))

        time.sleep(0.15)  # polite pacing

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

    # Record shell signal (we can later fill 'mfe' once OHLC learning is added)
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
