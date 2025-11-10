#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
analyze_hourly_reversal.py
v1.2 — Hourly adaptive reversal analyzer

Goal:
- Fetch Binance hourly klines since Jan 2023 for BTC/ETH/SOL.
- Test z-score thresholds (1.5–3.5) and holding periods (12–96h)
  for contrarian entry logic (overbought → short, oversold → long).
- Compute per-trade ROI, MAE, MFE, and compounded bankroll at 1× and 10× leverage.
- Print summary and best-performing configurations.

Output:
  Top results by compounded ROI and a detailed list of the top config’s trades.
"""

import requests
import time
from datetime import datetime, timedelta, timezone
import statistics as stats

# ───────────────────────────────────────────────────────────────
# Configuration
# ───────────────────────────────────────────────────────────────
BASES = [
    "https://api.binance.com",
    "https://api1.binance.com",
    "https://api2.binance.com",
    "https://api3.binance.com",
    "https://data-api.binance.vision",
]
HEADERS = {"User-Agent": "hourly-reversal-analyzer/1.2 (+github actions)"}
SYMBOLS = ["BTCUSDT", "ETHUSDT", "SOLUSDT"]
START_DATE = datetime(2023, 1, 1, tzinfo=timezone.utc)
END_DATE = datetime.now(timezone.utc)
THRESHOLDS = [1.5, 2.0, 2.5, 3.0, 3.5]
HOLD_HOURS = [12, 24, 48, 72, 96]
STOP_PCT = 0.03

# ───────────────────────────────────────────────────────────────
# Data fetcher (robust pagination)
# ───────────────────────────────────────────────────────────────
def binance_klines_1h(symbol, start_dt, end_dt):
    """
    Robust 1h pagination — continues strictly by time until end_dt.
    """
    PAGE_LIMIT = 1000
    out = []
    start_ms = int(start_dt.timestamp() * 1000)
    hard_end_ms = int(end_dt.timestamp() * 1000)
    last_err = None
    backoff = 0.25

    while start_ms <= hard_end_ms:
        params = {
            "symbol": symbol,
            "interval": "1h",
            "limit": PAGE_LIMIT,
            "startTime": start_ms,
        }
        got = None
        for base in BASES:
            try:
                r = requests.get(f"{base}/api/v3/klines", params=params, headers=HEADERS, timeout=30)
                if r.status_code in (451, 403):
                    last_err = Exception(f"{r.status_code} {r.reason}")
                    continue
                r.raise_for_status()
                got = r.json()
                break
            except Exception as e:
                last_err = e
                continue

        if got is None:
            time.sleep(backoff)
            backoff = min(2.0, backoff * 1.7)
            continue

        if not got:
            break

        for k in got:
            ct = int(k[6]) // 1000
            if ct > hard_end_ms:
                break
            out.append({
                "t": datetime.utcfromtimestamp(ct).replace(tzinfo=timezone.utc),
                "o": float(k[1]), "h": float(k[2]), "l": float(k[3]), "c": float(k[4])
            })

        last_ct_ms = int(got[-1][6])
        next_ms = last_ct_ms + 1
        if next_ms <= start_ms:
            next_ms = start_ms + 60 * 60 * 1000
        start_ms = next_ms
        time.sleep(0.05)

    ded = {}
    for x in out:
        ded[x["t"]] = x
    arr = sorted(ded.values(), key=lambda x: x["t"])
    return [x for x in arr if start_dt <= x["t"] <= end_dt]

# ───────────────────────────────────────────────────────────────
# Calculations
# ───────────────────────────────────────────────────────────────
def pct_returns(closes):
    return [closes[i] / closes[i-1] - 1.0 for i in range(1, len(closes))]

def zscore_series(r, look=20):
    zs = []
    for i in range(len(r)):
        if i + 1 < look:
            zs.append(None)
            continue
        window = r[i + 1 - look : i + 1]
        mu = sum(window) / len(window)
        sd = stats.pstdev(window)
        zs.append((r[i] - mu) / sd if sd else None)
    return zs

# ───────────────────────────────────────────────────────────────
# Backtest core
# ───────────────────────────────────────────────────────────────
def backtest(symbol, data, thresh, hold_h):
    closes = [x["c"] for x in data]
    times = [x["t"] for x in data]
    r = pct_returns(closes)
    zs = zscore_series(r, 20)
    trades = []
    bal = 100.0

    i = 20
    while i < len(r):
        z = zs[i]
        if z is None:
            i += 1
            continue
        ret = r[i]
        dir = None
        if z > thresh and ret > 0:
            dir = "SHORT"
        elif z < -thresh and ret < 0:
            dir = "LONG"

        if dir:
            entry = closes[i+1]
            entry_t = times[i+1]
            stop_price = entry * (1 - STOP_PCT if dir == "LONG" else 1 + STOP_PCT)
            target = entry * (1 + 0.02 if dir == "LONG" else 1 - 0.02)
            exit_idx = min(i + 1 + hold_h, len(closes) - 1)
            exit_t = times[exit_idx]
            exit_price = closes[exit_idx]
            mae = mfe = roi = 0
            for j in range(i+1, exit_idx+1):
                low, high = data[j]["l"], data[j]["h"]
                if dir == "LONG":
                    change = (high/entry - 1)*100
                    mfe = max(mfe, change)
                    mae = min(mae, (low/entry - 1)*100)
                    if low <= stop_price:
                        roi = -STOP_PCT*100
                        exit_price = stop_price
                        exit_t = times[j]
                        break
                    if high >= target:
                        roi = 2.0
                        exit_price = target
                        exit_t = times[j]
                        break
                else:
                    change = (entry/low - 1)*100
                    mfe = max(mfe, change)
                    mae = min(mae, (entry/high - 1)*100)
                    if high >= stop_price:
                        roi = -STOP_PCT*100
                        exit_price = stop_price
                        exit_t = times[j]
                        break
                    if low <= target:
                        roi = 2.0
                        exit_price = target
                        exit_t = times[j]
                        break
            if roi == 0:
                roi = (exit_price/entry - 1)*100 if dir == "LONG" else (entry/exit_price - 1)*100

            bal *= (1 + roi/100)
            trades.append({
                "symbol": symbol,
                "dir": dir,
                "entry": entry,
                "exit": exit_price,
                "entry_t": entry_t,
                "exit_t": exit_t,
                "roi": roi,
                "mae": mae,
                "mfe": mfe,
                "after": bal,
            })
            i = exit_idx + 1
        else:
            i += 1
    return trades

# ───────────────────────────────────────────────────────────────
# Main runner
# ───────────────────────────────────────────────────────────────
def main():
    results = []
    print("Fetching hourly data…")
    hourly_data = {}
    for sym in SYMBOLS:
        print(f"Downloading {sym} 1h…")
        hourly_data[sym] = binance_klines_1h(sym, START_DATE, END_DATE)
        print(f"  Got {len(hourly_data[sym])} bars from {hourly_data[sym][0]['t']} → {hourly_data[sym][-1]['t']}")

    for sym in SYMBOLS:
        data = hourly_data[sym]
        for zt in THRESHOLDS:
            for hh in HOLD_HOURS:
                t = backtest(sym, data, zt, hh)
                if not t:
                    continue
                roi = (t[-1]["after"] / 100) - 1
                results.append({
                    "sym": sym, "thresh": zt, "hold": hh,
                    "trades": len(t),
                    "roi": roi,
                    "final": t[-1]["after"], "tradeset": t
                })

    # rank by final balance
    top = sorted(results, key=lambda x: x["final"], reverse=True)[:10]
    print("\n=== Top 10 configurations (1× leverage) ===")
    for i, r in enumerate(top, 1):
        print(f"{i:2d}. {r['sym']} z≥{r['thresh']} hold={r['hold']}h → {r['final']:.2f} ({r['roi']*100:.2f}%)  trades={r['trades']}")

    best = top[0]
    print(f"\n=== Best Config Detailed Trades ({best['sym']} z≥{best['thresh']} hold={best['hold']}h) ===")
    for i, t in enumerate(best["tradeset"], 1):
        print(f"{i:2d} {t['symbol']} {t['dir']:5}  {t['entry_t']}  {t['exit_t']}  ROI={t['roi']:.2f}%  After=${t['after']:.2f}")

    lev_final = best["final"] * (10 / 1)
    print(f"\nWith 10× leverage: Final ≈ ${lev_final:.2f} (ROI ×10 before compounding risk).")

if __name__ == "__main__":
    main()
