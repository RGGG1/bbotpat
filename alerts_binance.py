#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
alerts_binance.py
v2.0 – Binance adaptive signals + Telegram dashboard

Features:
- Uses Binance public daily klines with mirror fallback.
- Computes z-score of 20-day returns.
- Converts to 0-100 “heat level” (50 = neutral).
- Flags >75 overbought, <25 oversold.
- Sends full Telegram report daily:
    • Current overbought/oversold status for BTC, ETH, SOL
    • Thresholds remaining before signal triggers
    • Active trade section if triggered
"""

import os
import json
import time
import requests
from datetime import datetime, timezone, timedelta

# ────────────────────────────────────────────────
# Configuration
# ────────────────────────────────────────────────
COINS = [("BTCUSDT", "BTC"), ("ETHUSDT", "ETH"), ("SOLUSDT", "SOL")]
Z_THRESH = 2.5                # z-score threshold for signal
SL = 0.03                     # stop loss (3%)
HOLD_BARS = 4                 # 96h = 4 daily candles
STATE_FILE = "adaptive_alerts_state.json"

# Fallback TPs (adaptive learning placeholder)
TP_FALLBACK = {"BTC": 0.0227, "ETH": 0.0167, "SOL": 0.0444}

# Telegram setup (use GitHub secrets or environment)
TG_BOT_TOKEN = os.getenv("TG_BOT_TOKEN")
TG_CHAT_ID   = os.getenv("TG_CHAT_ID")

BASES = [
    "https://api.binance.com",
    "https://api1.binance.com",
    "https://api2.binance.com",
    "https://api3.binance.com",
    "https://data-api.binance.vision"
]
HEADERS = {"User-Agent": "crypto-alert-bot/2.0 (+github actions)"}


# ────────────────────────────────────────────────
# Utility functions
# ────────────────────────────────────────────────
def binance_daily(symbol):
    """Download daily candles with fallback endpoints."""
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
        except Exception as e:
            last_err = e
            continue
    raise last_err if last_err else RuntimeError("All Binance bases failed")


def pct_returns(closes):
    return [closes[i] / closes[i - 1] - 1 for i in range(1, len(closes))]


def zscore_series(r, look=20):
    zs = []
    for i in range(len(r)):
        if i + 1 < look:
            zs.append(None)
            continue
        window = r[i + 1 - look : i + 1]
        mu = sum(window) / len(window)
        var = sum((x - mu) ** 2 for x in window) / len(window)
        sd = var ** 0.5
        zs.append(abs((r[i] - mu) / sd) if sd and sd > 0 else None)
    return zs


def median(values):
    v = sorted([x for x in values if x is not None])
    n = len(v)
    if n == 0:
        return None
    if n % 2 == 1:
        return v[n // 2]
    return (v[n // 2 - 1] + v[n // 2]) / 2.0


def median_mfe_for_coin(sym, state):
    hist = state.get("signals", {}).get(sym, [])
    mfes = [x.get("mfe") for x in hist if x.get("mfe") is not None]
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
    if not TG_BOT_TOKEN or not TG_CHAT_ID:
        print("⚠️ Telegram vars missing; message would be:\n", text)
        return
    try:
        url = f"https://api.telegram.org/bot{TG_BOT_TOKEN}/sendMessage"
        requests.post(url, json={"chat_id": TG_CHAT_ID, "text": text}, timeout=20)
    except Exception as e:
        print("Telegram post error:", e)
        print("Message was:\n", text)


# ────────────────────────────────────────────────
# Main logic
# ────────────────────────────────────────────────
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
            return
        else:
            state["active_until"] = None

    # ----- Evaluate each coin -----
    summary_lines = []
    candidates = []

    for symbol, sym in COINS:
        try:
            data = binance_daily(symbol)
        except Exception as e:
            summary_lines.append(f"{sym}: ⚠️ Data error ({e})")
            continue

        dates, closes = zip(*data)
        if len(closes) < 25:
            summary_lines.append(f"{sym}: Insufficient data")
            continue

        r = pct_returns(list(closes))
        zs = zscore_series(r, 20)
        if not zs or zs[-1] is None:
            summary_lines.append(f"{sym}: No recent data")
            continue

        z = zs[-1]
        recent_return = r[-1]
        # Direction-aware z (positive for up, negative for down)
        z_signed = z if recent_return > 0 else -z
        level = max(0, min(100, round(50 + z_signed * 20)))  # scale around 50

        if level >= 75:
            emoji = "🔴"
            desc = f"Overbought {level}% (needs ≥90% to short)"
        elif level <= 25:
            emoji = "🟢"
            desc = f"Oversold {level}% (needs ≤10% to long)"
        else:
            emoji = "⚪"
            desc = f"Neutral {level}%"
        summary_lines.append(f"{emoji} {sym}: {desc}")

        # --- Signal trigger logic ---
        if z >= Z_THRESH:
            direction = "SHORT" if recent_return > 0 else "LONG"
            tp = median_mfe_for_coin(sym, state)
            entry = closes[-1]
            entry_date = dates[-1]
            valid_until = datetime.combine(
                entry_date, datetime.min.time(), tzinfo=timezone.utc
            ) + timedelta(days=HOLD_BARS)
            candidates.append((sym, direction, entry, tp, entry_date, valid_until))

        time.sleep(0.15)

    # ----- Build Telegram message -----
    today = datetime.utcnow().strftime("%b %d, %Y")
    header = f"📊 Daily Crypto Report — {today}"
    summary = "\n".join(summary_lines)

    if not candidates:
        msg = f"{header}\n\n{summary}\n\nNo trades today."
    else:
        # pick highest priority (BTC > ETH > SOL)
        priority = {"BTC": 0, "ETH": 1, "SOL": 2}
        candidates.sort(key=lambda x: priority.get(x[0], 99))
        sym, direction, entry, tp, entry_date, valid_until = candidates[0]
        msg = (
            f"{header}\n\n{summary}\n\n"
            f"✅ *Active Trade: {sym}*\n"
            f"Direction: {direction}\n"
            f"Entry: {entry:.2f} USD\n"
            f"TP: {tp*100:.2f}% | SL: 3.00%\n"
            f"Hold: {HOLD_BARS*24}h\n"
            f"Valid until: {valid_until.isoformat()}"
        )

        state.setdefault("signals", {}).setdefault(sym, []).append({
            "date": str(entry_date),
            "direction": direction,
            "entry": entry,
            "tp_used": tp,
            "mfe": None
        })
        state["active_until"] = valid_until.isoformat()

    post_telegram(msg)
    save_state(state)
    print(msg)


# ────────────────────────────────────────────────
if __name__ == "__main__":
    main()
