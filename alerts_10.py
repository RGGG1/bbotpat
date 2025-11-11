#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
alerts_10.py — High-confidence extreme-move contrarian alerts using Binance daily candles.

Adds a daily STATUS BOARD:
  🟢 overbought, 🔴 oversold, ⚪ neutral  + token + latest daily close

Trade block (only if triggered):
  token, direction, entry, SL price & %, TP price & %, max hold

- Data: Binance public klines (1d), multi-endpoint fallback
- Coins: BTC, ETH, SOL, BNB, XRP
- Trigger: |daily move| >= coin threshold (historical ~90% next-day reversal)
    BTC:13%, ETH:14%, SOL:17%, BNB:20%, XRP:15%
- Direction: contrarian (SHORT after up day, LONG after down day)
- TP: coin-specific (ETH≈4.34%, SOL≈7.87%; others fallback 6.5%)
- SL: 5% (underlying)
- Hold cap: 96h (4 daily bars)
- No overlapping: skip new signals while active
- Delivery: Telegram message

ENV:
  TG_BOT_TOKEN, TG_CHAT_ID, ALERTS_NAME (optional)
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
    ("BNBUSDT", "BNB"),
    ("XRPUSDT", "XRP"),
]

THRESHOLDS_PCT = {"BTC": 13, "ETH": 14, "SOL": 17, "BNB": 20, "XRP": 15}  # absolute % move day over day

COIN_TP = { "ETH": 0.0434, "SOL": 0.0787 }  # fractions
TP_FALLBACK = 0.065
SL = 0.05
HOLD_BARS = 4
STATE_FILE = "alerts10_extreme_state.json"

TG_BOT_TOKEN = os.getenv("TG_BOT_TOKEN")
TG_CHAT_ID   = os.getenv("TG_CHAT_ID")
LABEL        = os.getenv("ALERTS_NAME", "ALGO10")

BASES = [
    "https://api.binance.com",
    "https://api1.binance.com",
    "https://api2.binance.com",
    "https://api3.binance.com",
    "https://data-api.binance.vision",
]
HEADERS = { "User-Agent": "alerts10/1.2 (+https://github.com)" }

# ──────────────────────────────────────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────────────────────────────────────
def binance_daily(symbol: str) -> List[Tuple[datetime.date, float]]:
    last_err = None
    for base in BASES:
        try:
            url = f"{base}/api/v3/klines"
            params = {"symbol": symbol, "interval": "1d", "limit": 1500}
            r = requests.get(url, params=params, headers=HEADERS, timeout=30)
            r.raise_for_status()
            data = r.json()
            rows = []
            for k in data:
                close_ts = int(k[6]) // 1000
                close_price = float(k[4])
                rows.append((datetime.utcfromtimestamp(close_ts).date(), close_price))
            return rows
        except requests.HTTPError as e:
            last_err = e
            code = e.response.status_code if e.response is not None else None
            if code in (451,403,429,520,521,522,523,524):
                continue
            raise
        except Exception as e:
            last_err = e
            continue
    raise last_err if last_err else RuntimeError("All Binance bases failed")

def post_telegram(text: str):
    msg = f"[{LABEL}] {text}"
    if not TG_BOT_TOKEN or not TG_CHAT_ID:
        print("Telegram env vars missing; printing message:\n", msg)
        return
    url = f"https://api.telegram.org/bot{TG_BOT_TOKEN}/sendMessage"
    try:
        requests.post(url, json={"chat_id": TG_CHAT_ID, "text": msg}, timeout=20)
    except Exception as e:
        print("Telegram post error:", e)
        print("Message was:\n", msg)

def fmt_price(p: float) -> str:
    if p >= 1000: return f"{p:,.0f}"
    if p >= 100:  return f"{p:,.2f}"
    if p >= 1:    return f"{p:,.3f}"
    return f"{p:.6f}"

# ──────────────────────────────────────────────────────────────────────────────
# Main
# ──────────────────────────────────────────────────────────────────────────────
def main():
    # Load state (for overlap window)
    try:
        with open(STATE_FILE, "r") as f:
            state = json.load(f)
    except Exception:
        state = {"active_until": None, "last_run": None}
    state["last_run"] = datetime.now(timezone.utc).isoformat()

    active_until = state.get("active_until")
    if active_until:
        try:
            dt_until = datetime.fromisoformat(active_until)
        except Exception:
            dt_until = None
        if dt_until and datetime.now(timezone.utc) < dt_until:
            # Active -> STATUS only
            status_lines = []
            for symbol, sym in COINS:
                rows = binance_daily(symbol)
                (d0, p0), (d1, p1) = rows[-2], rows[-1]
                dayret = p1/p0 - 1.0
                thr = THRESHOLDS_PCT[sym]
                emoji = "🟢" if (dayret*100 >=  thr) else ("🔴" if (dayret*100 <= -thr) else "⚪")
                status_lines.append(f"{emoji} {sym}: close {fmt_price(p1)}")
                time.sleep(0.1)
            post_telegram("Status only (active window):\n" + "\n".join(status_lines) + f"\nActive until: {dt_until.isoformat()}")
            with open(STATE_FILE, "w") as f: json.dump(state, f, default=str, indent=2)
            return
        else:
            state["active_until"] = None

    # Build STATUS + candidates
    status_lines = []
    candidates = []
    for symbol, sym in COINS:
        try:
            rows = binance_daily(symbol)
        except Exception as e:
            status_lines.append(f"⚪ {sym}: data error: {e}")
            continue

        (d0, p0), (d1, p1) = rows[-2], rows[-1]
        dayret = p1/p0 - 1.0
        thr = THRESHOLDS_PCT[sym]

        # Status emoji
        emoji = "🟢" if (dayret*100 >=  thr) else ("🔴" if (dayret*100 <= -thr) else "⚪")
        status_lines.append(f"{emoji} {sym}: close {fmt_price(p1)}")

        # Trigger?
        if abs(dayret)*100 >= thr:
            direction = "SHORT" if dayret > 0 else "LONG"
            tp = COIN_TP.get(sym, TP_FALLBACK)   # fraction
            entry = p1
            entry_date = d1
            valid_until = datetime.combine(entry_date, datetime.min.time(), tzinfo=timezone.utc) + timedelta(days=HOLD_BARS)
            candidates.append((sym, direction, entry, tp, entry_date, valid_until))

        time.sleep(0.15)

    # No triggers → status only
    if not candidates:
        post_telegram("Status:\n" + "\n".join(status_lines) + "\nNo trades today.")
        with open(STATE_FILE, "w") as f: json.dump(state, f, default=str, indent=2)
        return

    # No-overlap portfolio rule: choose first by priority
    priority = {"BTC": 0, "ETH": 1, "SOL": 2, "BNB": 3, "XRP": 4}
    candidates.sort(key=lambda x: priority.get(x[0], 99))
    sym, direction, entry, tp, entry_date, valid_until = candidates[0]

    # SL/TP prices
    if direction == "LONG":
        sl_price = entry * (1 - SL); tp_price = entry * (1 + tp)
    else:
        sl_price = entry * (1 + SL); tp_price = entry * (1 - tp)

    msg = (
        "Status:\n" + "\n".join(status_lines) + "\n\n"
        "Trade:\n"
        f"{sym} — {direction}\n"
        f"Entry: {fmt_price(entry)}\n"
        f"SL: {fmt_price(sl_price)}  ({SL*100:.2f}%)\n"
        f"TP: {fmt_price(tp_price)}  ({tp*100:.2f}%)\n"
        f"Max hold: {HOLD_BARS*24}h\n"
        f"No overlap; next signal after: {valid_until.isoformat()}"
    )
    post_telegram(msg)

    state["active_until"] = valid_until.isoformat()
    with open(STATE_FILE, "w") as f: json.dump(state, f, default=str, indent=2)

if __name__ == "__main__":
    main()
