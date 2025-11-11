#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
v1.3 (Binance with multi-endpoint + mirror fallback)
Adaptive reversal alerts (BTC/ETH/SOL) using Binance daily candles.

Adds a daily STATUS BOARD:
  🟢 overbought, 🔴 oversold, ⚪ neutral  + token + latest daily close

Trade block (only if triggered):
  token, direction, entry, SL price & %, TP price & %, max hold

- Data: Binance public klines (1d), with fallback to api1/api2/api3 + data-api.binance.vision
- Trigger: |z| >= 2.5 on 20-day return z-score (close-to-close)
- Direction: contrarian (SHORT after up day, LONG after down day)
- TP: per-coin rolling median MFE within 96h (fallback TP used until learned)
- SL: 3% (underlying)
- Hold cap: 96h (4 daily bars)
- No overlapping: skip new signals during active window
- Delivery: Telegram message (status always; trade section only if triggered)

ENV VARS:
  TG_BOT_TOKEN  - Telegram bot token from @BotFather
  TG_CHAT_ID    - your chat id (integer)
  ALERTS_NAME   - label prefix in messages (optional)
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
LABEL        = os.getenv("ALERTS_NAME", "BINANCE")

BASES = [
    "https://api.binance.com",
    "https://api1.binance.com",
    "https://api2.binance.com",
    "https://api3.binance.com",
    "https://data-api.binance.vision",  # public mirror
]
HEADERS = { "User-Agent": "alerts-bot/1.3 (+https://github.com)" }

# ──────────────────────────────────────────────────────────────────────────────
# Data fetch — Binance 1d klines with multi-endpoint fallback
# ──────────────────────────────────────────────────────────────────────────────
def binance_daily(symbol: str) -> List[Tuple[datetime.date, float]]:
    """
    Returns list of (dateUTC, close) tuples for the given Binance symbol.
    """
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
                close_ts = int(k[6]) // 1000  # closeTime (ms) -> s
                close_price = float(k[4])     # close
                rows.append((datetime.utcfromtimestamp(close_ts).date(), close_price))
            return rows
        except requests.HTTPError as e:
            last_err = e
            code = e.response.status_code if e.response is not None else None
            if code in (451, 403, 429, 520, 521, 522, 523, 524):
                continue
            raise
        except Exception as e:
            last_err = e
            continue
    raise last_err if last_err else RuntimeError("All Binance bases failed")

# ──────────────────────────────────────────────────────────────────────────────
# Stats & signal
# ──────────────────────────────────────────────────────────────────────────────
def pct_returns(closes: List[float]) -> List[float]:
    return [closes[i]/closes[i-1]-1.0 for i in range(1, len(closes))]

def zscore_series(r: List[float], look: int = 20) -> List[float]:
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
    state = load_state()
    state["last_run"] = datetime.now(timezone.utc).isoformat()

    # If in active window → STATUS only
    active_until = state.get("active_until")
    if active_until:
        try:
            dt_until = datetime.fromisoformat(active_until)
        except Exception:
            dt_until = None
        if dt_until and datetime.now(timezone.utc) < dt_until:
            status_lines = []
            for symbol, sym in COINS:
                data = binance_daily(symbol)
                dates, closes = zip(*data)
                r = pct_returns(list(closes))
                zs = zscore_series(r, 20)
                ret = r[-1] if len(r) else 0.0
                close = closes[-1]
                emoji = "🟢" if (zs and zs[-1] is not None and zs[-1] >= Z_THRESH and ret > 0) else \
                        "🔴" if (zs and zs[-1] is not None and zs[-1] >= Z_THRESH and ret < 0) else "⚪"
                status_lines.append(f"{emoji} {sym}: close {fmt_price(close)}")
                time.sleep(0.1)
            post_telegram("Status only (active window):\n" + "\n".join(status_lines) + f"\nActive until: {dt_until.isoformat()}")
            save_state(state); print("Heartbeat: active window; status sent."); return
        else:
            state["active_until"] = None

    # Build STATUS + candidates
    status_lines = []
    candidates = []
    for symbol, sym in COINS:
        try:
            data = binance_daily(symbol)
        except Exception as e:
            status_lines.append(f"⚪ {sym}: data error: {e}")
            continue

        dates, closes = zip(*data)
        r = pct_returns(list(closes))
        zs = zscore_series(r, 20)
        close = closes[-1]
        ret = r[-1] if len(r) else 0.0

        emoji = "🟢" if (zs and zs[-1] is not None and zs[-1] >= Z_THRESH and ret > 0) else \
                "🔴" if (zs and zs[-1] is not None and zs[-1] >= Z_THRESH and ret < 0) else "⚪"
        status_lines.append(f"{emoji} {sym}: close {fmt_price(close)}")

        if zs and zs[-1] is not None and zs[-1] >= Z_THRESH:
            direction = "SHORT" if ret > 0 else "LONG"
            tp = median_mfe_for_coin(sym, state)  # fraction
            entry = close
            entry_date = dates[-1]
            valid_until = datetime.combine(entry_date, datetime.min.time(), tzinfo=timezone.utc) + timedelta(days=HOLD_BARS)
            candidates.append((sym, direction, entry, tp, entry_date, valid_until))

        time.sleep(0.15)

    if not candidates:
        post_telegram("Status:\n" + "\n".join(status_lines) + "\nNo trades today.")
        save_state(state); print("No signals; status sent."); return

    priority = {"BTC": 0, "ETH": 1, "SOL": 2}
    candidates.sort(key=lambda x: priority.get(x[0], 99))
    sym, direction, entry, tp, entry_date, valid_until = candidates[0]

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

    state.setdefault("signals", {}).setdefault(sym, []).append({
        "date": str(entry_date), "direction": direction, "entry": float(entry),
        "tp_used": float(tp), "mfe": None
    })
    state["active_until"] = valid_until.isoformat()
    save_state(state); print("Alert sent.")

if __name__ == "__main__":
    main()
