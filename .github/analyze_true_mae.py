#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Analyze true MAE/MFE for the *actual* trades your live logic would take:
- BTC > ETH > SOL priority
- Only one trade active at a time
- Entry on daily 77% signal (anchor = fully-closed daily close)
- Exit on TP hit (fallback TP per coin) or 96h expiry
- Hourly path for MAE/MFE

Outputs mae_true.csv. Prints a concise per-trade table and a summary.
"""

import csv, time, requests
from datetime import datetime, timezone, timedelta
from statistics import mean, pstdev

COINS = [("BTCUSDT","BTC",0.0227), ("ETHUSDT","ETH",0.0167), ("SOLUSDT","SOL",0.0444)]
CONF_TRIGGER = 77
HOLD_BARS    = 4  # 96h
START_DATE   = datetime(2023, 1, 1, tzinfo=timezone.utc)

BASES = [
    "https://api.binance.com",
    "https://api1.binance.com",
    "https://api2.binance.com",
    "https://api3.binance.com",
]
HEADERS = {"User-Agent": "true-mae/1.1 (+bbot)"}

# ---------- HTTP helpers ----------
def binance_klines(symbol, interval, limit=1500, end_time_ms=None, start_time_ms=None, tries=4):
    params = {"symbol": symbol, "interval": interval, "limit": limit}
    if end_time_ms is not None: params["endTime"]   = end_time_ms
    if start_time_ms is not None: params["startTime"] = start_time_ms
    last_err = None
    for _ in range(tries):
        for base in BASES:
            try:
                r = requests.get(f"{base}/api/v3/klines", params=params, headers=HEADERS, timeout=30)
                r.raise_for_status()
                return r.json()
            except Exception as e:
                last_err = e
                time.sleep(0.25)
    raise last_err if last_err else RuntimeError("All Binance bases failed")

def fully_closed_daily(symbol):
    # Return only completed daily candles up to today's 00:00 UTC - 1 ms
    now = datetime.utcnow().replace(tzinfo=timezone.utc)
    midnight = datetime(now.year, now.month, now.day, tzinfo=timezone.utc)
    end_ms = int(midnight.timestamp() * 1000) - 1
    ks = binance_klines(symbol, "1d", 1500, end_time_ms=end_ms)
    rows = []
    for k in ks:
        close_ts = int(k[6]) // 1000  # close time
        rows.append((datetime.utcfromtimestamp(close_ts).replace(tzinfo=timezone.utc), float(k[4])))
    return [(dt, px) for (dt, px) in rows if dt >= START_DATE]

def hourlies_between(symbol, start_dt, end_dt):
    out = []
    start_ms = int(start_dt.timestamp() * 1000)
    hard_end_ms = int((end_dt + timedelta(hours=1)).timestamp() * 1000)
    while True:
        part = binance_klines(symbol, "1h", 1500, start_time_ms=start_ms)
        if not part:
            break
        for k in part:
            ct = int(k[6]) // 1000
            dt = datetime.utcfromtimestamp(ct).replace(tzinfo=timezone.utc)
            if dt > end_dt:
                break
            out.append((dt, float(k[4])))
        start_ms = int(part[-1][6]) + 1
        if len(part) < 1500 or start_ms > hard_end_ms or (out and out[-1][0] >= end_dt):
            break
    # Deduplicate & filter
    dedup = {}
    for dt, px in out:
        dedup[dt] = px
    series = sorted((dt, px) for dt, px in dedup.items() if start_dt <= dt <= end_dt)
    return series

# ---------- math ----------
def pct_returns(closes):
    return [closes[i] / closes[i-1] - 1.0 for i in range(1, len(closes))]

def heat_series(closes, look=20):
    r = pct_returns(closes)
    out = [None] * len(closes)
    for i in range(len(closes)):
        if i < look:
            continue
        w = r[i - look:i]
        mu = mean(w)
        sd = pstdev(w) if len(w) > 1 else 0.0
        if sd <= 0:
            continue
        last_ret = r[i - 1]
        z = (last_ret - mu) / sd
        z_signed = z if last_ret > 0 else -z
        out[i] = max(0, min(100, round(50 + 20 * z_signed)))
    return out

# ---------- main ----------
def run():
    # Load daily series for each coin
    daily = {}
    for symbol, sym, tp in COINS:
        rows = fully_closed_daily(symbol)
        if len(rows) < 30:
            raise RuntimeError(f"{sym}: insufficient daily data after filtering.")
        dts, cls = zip(*rows)
        heats = heat_series(list(cls), 20)
        # Build dicts for O(1) lookup
        dt_to_idx = {d: i for i, d in enumerate(dts)}
        daily[sym] = {
            "symbol": symbol,
            "dates": list(dts),
            "closes": list(cls),
            "heat": heats,
            "idx": dt_to_idx,
            "tp": tp
        }
        time.sleep(0.15)

    # Build calendar of common dates where all have heat computed (not None)
    common_dates = set(daily["BTC"]["dates"]) & set(daily["ETH"]["dates"]) & set(daily["SOL"]["dates"])
    cal = []
    for d in sorted(common_dates):
        ok = True
        for sym in ["BTC", "ETH", "SOL"]:
            idx = daily[sym]["idx"].get(d, None)
            if idx is None or daily[sym]["heat"][idx] is None:
                ok = False
                break
        if ok:
            cal.append(d)

    if not cal:
        raise RuntimeError("No aligned dates with valid heat for all coins.")

    trades = []
    active = None

    for day in cal:
        # If a trade is active, we finish it immediately (we don’t let another day start overlapping)
        if active is not None:
            # Evaluate exit using hourly path
            sym = active["sym"]
            symbol = daily[sym]["symbol"]
            entry_dt = active["entry_dt"]
            entry_px = active["entry_px"]
            tp_pct = active["tp"]
            direction = active["direction"]

            valid_until = entry_dt + timedelta(days=HOLD_BARS)
            hours = hourlies_between(symbol, entry_dt, valid_until)
            if not hours:
                # Fallback: keep entry_dt/px; force expiry at 96h even if no bars (edge case)
                exit_dt = valid_until
                exit_px = entry_px
                mae = 0.0
                mfe = 0.0
                reason = "expiry(no-hourlies)"
            else:
                mae = 0.0
                mfe = 0.0
                exit_dt = hours[-1][0]
                exit_px = hours[-1][1]
                reason = "expiry"
                tp_price = entry_px * (1 + tp_pct) if direction == "LONG" else entry_px * (1 - tp_pct)

                for (dt, px) in hours:
                    move = (px / entry_px - 1.0) if direction == "LONG" else (entry_px / px - 1.0)
                    mfe = max(mfe, move)
                    mae = min(mae, move)
                    if direction == "LONG" and px >= tp_price:
                        exit_dt, exit_px, reason = dt, px, "tp"
                        break
                    if direction == "SHORT" and px <= tp_price:
                        exit_dt, exit_px, reason = dt, px, "tp"
                        break

            trades.append({
                "sym": sym,
                "direction": direction,
                "entry_dt": entry_dt,
                "exit_dt": exit_dt,
                "entry_px": entry_px,
                "exit_px": exit_px,
                "hold_h": int(round((exit_dt - entry_dt).total_seconds() / 3600)),
                "mae": mae * 100.0,
                "mfe": mfe * 100.0,
                "reason": reason
            })
            active = None  # free the slot for new signals on this day

        # If slot is free, check for a **new** signal today (priority BTC > ETH > SOL)
        if active is None:
            for sym in ["BTC", "ETH", "SOL"]:
                idx = daily[sym]["idx"][day]
                lvl = daily[sym]["heat"][idx]
                if lvl is None:
                    continue
                if lvl >= CONF_TRIGGER or lvl <= 100 - CONF_TRIGGER:
                    direction = "SHORT" if lvl >= CONF_TRIGGER else "LONG"
                    entry_px = daily[sym]["closes"][idx]
                    active = {
                        "sym": sym,
                        "symbol": daily[sym]["symbol"],
                        "direction": direction,
                        "entry_dt": day,        # model anchor = daily close time
                        "entry_px": entry_px,
                        "tp": daily[sym]["tp"]
                    }
                    break  # take the first by priority

    # Write results
    with open("mae_true.csv", "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["#","Symbol","Dir","EntryUTC","ExitUTC","HoldH","MAE%","MFE%","ExitReason"])
        for i, tr in enumerate(trades, 1):
            w.writerow([
                i, tr["sym"], tr["direction"],
                tr["entry_dt"].strftime("%Y-%m-%d %H:%M"),
                tr["exit_dt"].strftime("%Y-%m-%d %H:%M"),
                tr["hold_h"],
                f"{tr['mae']:.2f}",
                f"{tr['mfe']:.2f}",
                tr["reason"]
            ])

    # Console summary
    print(f"Wrote mae_true.csv with {len(trades)} trades\n")
    print(f"{'#':>3}  {'SYM':<3}  {'Dir':<5}  {'Entry UTC':<16}  {'Exit UTC':<16}  {'Hh':>4}  {'MAE%':>7}  {'MFE%':>7}  {'Exit'}")
    for i, tr in enumerate(trades, 1):
        print(f"{i:>3}  {tr['sym']:<3}  {tr['direction']:<5}  "
              f"{tr['entry_dt'].strftime('%Y-%m-%d %H:%M'):16}  "
              f"{tr['exit_dt'].strftime('%Y-%m-%d %H:%M'):16}  "
              f"{tr['hold_h']:4d}  {tr['mae']:7.2f}  {tr['mfe']:7.2f}  {tr['reason']}")
    if trades:
        mae_median = sorted(x["mae"] for x in trades)[len(trades)//2]
        print(f"\nMedian MAE across real trades: {mae_median:.2f}%")
        print("Tip: try limit entries at anchor improved by ~median MAE "
              "(anchor*(1-μ) for LONG, anchor*(1+μ) for SHORT).")

if __name__ == "__main__":
    run()
    
