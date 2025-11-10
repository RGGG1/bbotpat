#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
alerts_binance.py  — Golden "44×" setup (edge-cross 90/10, BTC>ETH>SOL, single-bankroll semantics)

What this does (matches your best-performing, low-RoR variant):
- Universe: BTC, ETH, SOL only
- Signal: 20-day return z-score → “heat” in [0..100]
  * Enter on an **edge-cross** into extremes **today vs yesterday**:
      - SHORT when heat >= 90 and yesterday < 90 (after an up day)
      - LONG  when heat <= 10 and yesterday > 10 (after a down day)
- Entry: last **fully closed daily** close (anchor)
- Exit (advisory): TP at adaptive per-coin median MFE within 96h (fallbacks used until >=5 samples)
                   Expiry after 96h if no TP hit
- Portfolio semantics: one trade at a time. If a trade closes and a new one opens on the same daily close, that’s allowed.
- Telegram: summary lines with emoji + confidence + close price; active trade block when a new one opens.

Note: This file **advises** entries/exits; it does not auto-trade and does not enforce SL. SL is informational only.
"""

import os
import json
import time
import requests
from datetime import datetime, timezone, timedelta
from statistics import mean, pstdev, median

# ────────────────────────────────────────────────
# Config
# ────────────────────────────────────────────────
COINS = [("BTCUSDT","BTC"), ("ETHUSDT","ETH"), ("SOLUSDT","SOL")]

HEAT_LONG  = 10     # <=10 → LONG (when crossed from >10 yesterday)
HEAT_SHORT = 90     # >=90 → SHORT (when crossed from <90 yesterday)
LOOKBACK   = 20     # 20-day return z
HOLD_BARS  = 4      # 96h cap (advisory window)
STATE_FILE = "adaptive_alerts_state.json"

TP_FALLBACK = {"BTC":0.0227, "ETH":0.0167, "SOL":0.0444}  # % as fraction
SL_INFO     = 0.03  # informational only

# Telegram
TG_BOT_TOKEN = os.getenv("TG_BOT_TOKEN")
TG_CHAT_ID   = os.getenv("TG_CHAT_ID")

# Binance endpoints (with mirror)
BASES = [
    "https://data-api.binance.vision",
    "https://api.binance.com",
    "https://api1.binance.com",
    "https://api2.binance.com",
    "https://api3.binance.com",
]
HEADERS = {"User-Agent": "alerts-44x/1.0 (+github actions)"}

# ────────────────────────────────────────────────
# HTTP helpers
# ────────────────────────────────────────────────
def binance_klines(symbol, interval, limit=1500, end_time_ms=None, start_time_ms=None, tries=6):
    params = {"symbol":symbol, "interval":interval, "limit":limit}
    if end_time_ms is not None: params["endTime"]   = end_time_ms
    if start_time_ms is not None: params["startTime"] = start_time_ms
    last_err=None; backoff=0.25
    for _ in range(tries):
        for base in BASES:
            try:
                r = requests.get(f"{base}/api/v3/klines", params=params, headers=HEADERS, timeout=30)
                if r.status_code in (451,403):
                    last_err = requests.HTTPError(f"{r.status_code} {r.reason}")
                    continue
                r.raise_for_status()
                return r.json()
            except Exception as e:
                last_err=e; time.sleep(backoff)
        backoff=min(2.0, backoff*1.8)
    raise last_err if last_err else RuntimeError("All Binance bases failed")

def fully_closed_daily(symbol):
    """
    Return list of (dateUTC, close) for completed daily candles up to yesterday.
    """
    now = datetime.utcnow().replace(tzinfo=timezone.utc)
    y   = now - timedelta(days=1)
    end_ms = int(datetime(y.year,y.month,y.day,23,59,59,tzinfo=timezone.utc).timestamp()*1000)+999
    ks = binance_klines(symbol, "1d", 1500, end_time_ms=end_ms)
    rows = []
    for k in ks:
        ct = int(k[6])//1000
        rows.append((datetime.utcfromtimestamp(ct).date(), float(k[4])))
    return rows

# ────────────────────────────────────────────────
# Stats
# ────────────────────────────────────────────────
def pct_returns(closes):
    return [closes[i]/closes[i-1]-1.0 for i in range(1,len(closes))]

def heat_series_aligned_to_day(closes, look=LOOKBACK):
    """
    Heat aligned to *day i* (uses r[i-look+1 : i] to score r[i]).
    Direction-aware: up-day => positive z; down-day => negative z.
    Heat = 50 + 20 * signed_z, clipped to [0,100].
    """
    r = pct_returns(closes)
    out = [None]*len(closes)
    for i in range(len(closes)):
        if i < look or i >= len(closes)-1:  # need r[i] defined and full window
            continue
        window = r[i-look+1:i+1]
        if len(window) != look: continue
        mu = mean(window)
        sd = pstdev(window) if len(window)>1 else 0.0
        if sd <= 0: continue
        last_ret = r[i]
        z = (last_ret - mu)/sd
        z_signed = z if last_ret>0 else -z
        out[i] = max(0, min(100, round(50 + 20*z_signed)))
    return out

def median_mfe_for_coin(sym, state):
    vals = state.get("mfes", {}).get(sym, [])
    if len(vals) >= 5:
        return median(vals)/100.0
    return TP_FALLBACK.get(sym, 0.03)

# ────────────────────────────────────────────────
# State & Telegram
# ────────────────────────────────────────────────
def load_state():
    try:
        with open(STATE_FILE,"r") as f: return json.load(f)
    except Exception:
        return {"mfes": {}, "last_run": None, "last_trade": None, "active_until": None}

def save_state(state):
    with open(STATE_FILE,"w") as f: json.dump(state, f, default=str, indent=2)

def post_telegram(text):
    if not TG_BOT_TOKEN or not TG_CHAT_ID:
        print("⚠️ (no TG env) would send:\n", text)
        return
    try:
        requests.post(f"https://api.telegram.org/bot{TG_BOT_TOKEN}/sendMessage",
                      json={"chat_id":TG_CHAT_ID,"text":text}, timeout=20)
    except Exception as e:
        print("Telegram error:", e, "\nMessage:\n", text)

# ────────────────────────────────────────────────
# Main
# ────────────────────────────────────────────────
def main():
    state = load_state()
    state["last_run"] = datetime.now(timezone.utc).isoformat()

    # Load fully-closed dailies and compute today's heat + yesterday heat
    summary_lines = []
    candidates = []  # (sym, direction, entry_px, entry_date, valid_until, heat_today)

    daily = {}
    for symbol, sym in COINS:
        try:
            rows = fully_closed_daily(symbol)
        except Exception as e:
            summary_lines.append(f"{sym}: ⚠️ data error ({e})")
            continue

        dates, closes = zip(*rows)
        heats = heat_series_aligned_to_day(list(closes), LOOKBACK)
        daily[sym] = {"symbol": symbol, "dates": list(dates), "closes": list(closes), "heats": heats}
        i = len(closes) - 1
        h_today = heats[i]
        h_yday  = heats[i-1] if i-1 >= 0 else None

        # Build nice summary with emoji + close + heat%
        close_px = closes[-1]
        if h_today is None:
            summary_lines.append(f"⚪ {sym}: Neutral — insufficient data | Close: ${close_px:,.2f}")
        elif h_today >= 75:
            summary_lines.append(f"🔴 {sym}: Overbought {int(h_today)}% | Close: ${close_px:,.2f}")
        elif h_today <= 25:
            summary_lines.append(f"🟢 {sym}: Oversold {int(h_today)}% | Close: ${close_px:,.2f}")
        else:
            summary_lines.append(f"⚪ {sym}: Neutral {int(h_today)}% | Close: ${close_px:,.2f}")

        # Edge-cross trigger (into extreme today vs yesterday)
        if h_today is not None and h_yday is not None:
            crossed_short = (h_today >= HEAT_SHORT) and (h_yday < HEAT_SHORT)
            crossed_long  = (h_today <= HEAT_LONG)  and (h_yday > HEAT_LONG)
            if crossed_short or crossed_long:
                # Contrarian direction comes from the sign of today's return
                r = pct_returns(list(closes))
                today_ret = r[-1]
                direction = "SHORT" if today_ret > 0 else "LONG"
                entry = closes[-1]
                entry_date = dates[-1]
                valid_until = datetime.combine(entry_date, datetime.min.time(), tzinfo=timezone.utc) + timedelta(days=HOLD_BARS)
                candidates.append((sym, direction, entry, entry_date, valid_until, int(h_today)))

        time.sleep(0.12)

    # Enforce "one trade at a time" window — advisory heartbeat if still within window
    active_until = state.get("active_until")
    if active_until:
        try:
            dt_until = datetime.fromisoformat(active_until)
        except Exception:
            dt_until = None
        if dt_until and datetime.now(timezone.utc) < dt_until:
            header = f"📊 Daily Crypto Report — {datetime.utcnow().strftime('%b %d, %Y')}"
            post_telegram(f"{header}\n\n" + "\n".join(summary_lines) +
                          f"\n\n🟨 No new trade (active window until {dt_until.isoformat()}).")
            save_state(state)
            print("Heartbeat sent (active window).")
            return
        else:
            state["active_until"] = None

    header = f"📊 Daily Crypto Report — {datetime.utcnow().strftime('%b %d, %Y')}"
    if not candidates:
        post_telegram(f"{header}\n\n" + "\n".join(summary_lines) + "\n\nNo trades today.")
        save_state(state)
        print("No trades today.")
        return

    # Pick one by fixed priority BTC > ETH > SOL
    pr = {"BTC":0,"ETH":1,"SOL":2}
    candidates.sort(key=lambda x: pr.get(x[0], 99))
    sym, direction, entry, entry_date, valid_until, heatp = candidates[0]

    # Compute current advisory TP
    tp_used = median_mfe_for_coin(sym, state)

    msg = (f"{header}\n\n" + "\n".join(summary_lines) + "\n\n" +
           f"🚨 TRADE SIGNAL: {sym}\n"
           f"Side: {direction}\n"
           f"Anchor entry (prev daily close): ${entry:,.2f}\n"
           f"Confidence (heat): {heatp}%\n"
           f"TP: {tp_used*100:.2f}%  | SL (info): {SL_INFO*100:.2f}%\n"
           f"Max hold: {HOLD_BARS*24}h\n"
           f"Valid until: {valid_until.isoformat()}")

    # Record last trade shell (mfe updating is done by your analyzer while backtesting)
    state["last_trade"] = {"sym": sym, "direction": direction, "entry": entry,
                           "tp_used": tp_used, "entry_date": str(entry_date)}
    state["active_until"] = valid_until.isoformat()

    post_telegram(msg)
    save_state(state)
    print(msg)

if __name__ == "__main__":
    main()
