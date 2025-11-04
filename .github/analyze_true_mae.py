#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
True MAE/MFE & ROI for the *actual trades* your '44x' configuration would take.
"""

import csv, time, requests
from statistics import mean, pstdev, median
from datetime import datetime, timezone, timedelta

# ---------------- Config ----------------
COINS = [
    ("BTCUSDT", "BTC", 0.0227),
    ("ETHUSDT", "ETH", 0.0167),
    ("SOLUSDT", "SOL", 0.0444),
]
CONF_TRIGGER = 77
LOOKBACK = 20
HOLD_BARS = 4
START_DATE = datetime(2023, 1, 1, tzinfo=timezone.utc)

BASES = [
    "https://data-api.binance.vision",
    "https://api.binance.com",
    "https://api1.binance.com",
    "https://api2.binance.com",
    "https://api3.binance.com",
]
HEADERS = {"User-Agent": "true-mae-77/1.0 (+bbot)"}

# ---------------- Helpers ----------------
def binance_klines(symbol, interval, limit=1500, end_time_ms=None, start_time_ms=None):
    params = {"symbol": symbol, "interval": interval, "limit": limit}
    if end_time_ms:
        params["endTime"] = end_time_ms
    if start_time_ms:
        params["startTime"] = start_time_ms

    last_err = None
    for base in BASES:
        try:
            r = requests.get(f"{base}/api/v3/klines", params=params, headers=HEADERS, timeout=30)
            if r.status_code in (451, 403):
                continue
            r.raise_for_status()
            return r.json()
        except Exception as e:
            last_err = e
            time.sleep(0.3)
    raise last_err if last_err else RuntimeError("All Binance bases failed")

def fully_closed_daily(symbol):
    now = datetime.utcnow().replace(tzinfo=timezone.utc)
    y = now - timedelta(days=1)
    end_ms = int(datetime(y.year, y.month, y.day, 23, 59, 59, tzinfo=timezone.utc).timestamp() * 1000) + 999
    ks = binance_klines(symbol, "1d", 1500, end_time_ms=end_ms)
    rows = []
    for k in ks:
        close_ts = int(k[6]) // 1000
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
    dedup = {}
    for dt, px in out:
        dedup[dt] = px
    return sorted((dt, px) for dt, px in dedup.items() if start_dt <= dt <= end_dt)

def pct_returns(closes):
    return [closes[i] / closes[i - 1] - 1.0 for i in range(1, len(closes))]

def heat_series(closes, look=20):
    r = pct_returns(closes)
    out = [None] * len(closes)
    for i in range(len(closes)):
        if i < look:
            continue
        w = r[i - look : i]
        mu = mean(w)
        sd = pstdev(w) if len(w) > 1 else 0.0
        if sd <= 0:
            continue
        last_ret = r[i - 1]
        z = (last_ret - mu) / sd
        z_signed = z if last_ret > 0 else -z
        out[i] = max(0, min(100, round(50 + 20 * z_signed)))
    return out

# ---------------- Main ----------------
def run():
    daily = {}
    for symbol, sym, tp_fallback in COINS:
        rows = fully_closed_daily(symbol)
        if len(rows) < 30:
            raise RuntimeError(f"{sym}: insufficient daily data.")
        dts, cls = zip(*rows)
        heats = heat_series(list(cls), look=LOOKBACK)
        daily[sym] = {
            "symbol": symbol,
            "dates": list(dts),
            "closes": list(cls),
            "heat": heats,
            "index": {d: i for i, d in enumerate(dts)},
            "tp_fallback": tp_fallback,
        }
        time.sleep(0.1)

    common = set(daily["BTC"]["dates"]) & set(daily["ETH"]["dates"]) & set(daily["SOL"]["dates"])
    cal = []
    for d in sorted(common):
        ok = True
        for sym in ("BTC", "ETH", "SOL"):
            i = daily[sym]["index"][d]
            if daily[sym]["heat"][i] is None:
                ok = False
                break
        if ok:
            cal.append(d)

    prior_mfe = {"BTC": [], "ETH": [], "SOL": []}
    def adaptive_tp(sym, fallback):
        vals = prior_mfe[sym]
        return median(vals)/100.0 if len(vals) >= 5 else fallback

    def edge_dir(sym, day):
        coin = daily[sym]
        i = coin["index"][day]
        if i == 0:
            return None
        t, y = coin["heat"][i], coin["heat"][i - 1]
        if t is None or y is None:
            return None
        if t >= CONF_TRIGGER and y < CONF_TRIGGER:
            return "SHORT"
        if t <= 100 - CONF_TRIGGER and y > 100 - CONF_TRIGGER:
            return "LONG"
        return None

    trades = []
    active = None

    for day in cal:
        if active:
            sym = active["sym"]
            symbol = daily[sym]["symbol"]
            entry_dt = active["entry_dt"]
            entry_px = active["entry_px"]
            tp_pct = active["tp_pct"]
            direction = active["direction"]
            valid_until = entry_dt + timedelta(days=HOLD_BARS)
            hours = hourlies_between(symbol, entry_dt, valid_until)

            mae = mfe = 0.0
            exit_dt, exit_px, reason = valid_until, entry_px, "expiry"
            if hours:
                tp_px = entry_px * (1 + tp_pct) if direction == "LONG" else entry_px * (1 - tp_pct)
                for (dt, px) in hours:
                    move = (px / entry_px - 1.0) if direction == "LONG" else (entry_px / px - 1.0)
                    mfe = max(mfe, move)
                    mae = min(mae, move)
                    if (direction == "LONG" and px >= tp_px) or (direction == "SHORT" and px <= tp_px):
                        exit_dt, exit_px, reason = dt, px, "tp"
                        break
            roi = (exit_px / entry_px - 1.0) if direction == "LONG" else (entry_px / exit_px - 1.0)
            trades.append({
                "sym": sym,
                "direction": direction,
                "entry_dt": entry_dt,
                "exit_dt": exit_dt,
                "entry_px": entry_px,
                "exit_px": exit_px,
                "hold_h": int(round((exit_dt - entry_dt).total_seconds() / 3600)),
                "mae_pct": mae * 100.0,
                "mfe_pct": mfe * 100.0,
                "roi_pct": roi * 100.0,
                "reason": reason,
            })
            prior_mfe[sym].append(mfe * 100.0)
            active = None

        if not active:
            for sym in ("BTC", "ETH", "SOL"):
                direction = edge_dir(sym, day)
                if direction:
                    i = daily[sym]["index"][day]
                    entry_px = daily[sym]["closes"][i]
                    tp_pct = adaptive_tp(sym, daily[sym]["tp_fallback"])
                    active = {
                        "sym": sym,
                        "symbol": daily[sym]["symbol"],
                        "direction": direction,
                        "entry_dt": day,
                        "entry_px": entry_px,
                        "tp_pct": tp_pct,
                    }
                    break

    with open("mae_true.csv", "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["#", "Symbol", "Dir", "EntryUTC", "ExitUTC", "HoldH", "EntryPx", "ExitPx", "MAE%", "MFE%", "ROI%", "ExitReason"])
        for i, tr in enumerate(trades, 1):
            w.writerow([
                i, tr["sym"], tr["direction"],
                tr["entry_dt"].strftime("%Y-%m-%d %H:%M"),
                tr["exit_dt"].strftime("%Y-%m-%d %H:%M"),
                tr["hold_h"],
                f"{tr['entry_px']:.6f}",
                f"{tr['exit_px']:.6f}",
                f"{tr['mae_pct']:.2f}",
                f"{tr['mfe_pct']:.2f}",
                f"{tr['roi_pct']:.2f}",
                tr["reason"],
            ])

    print(f"Wrote mae_true.csv with {len(trades)} trades\n")
    print(f"{'#':>3}  {'SYM':<3}  {'Dir':<5}  {'Entry UTC':<16}  {'Exit UTC':<16}  {'Hh':>4}  {'MAE%':>7}  {'MFE%':>7}  {'ROI%':>7}  {'Exit'}")
    for i, tr in enumerate(trades, 1):
        print(f"{i:>3}  {tr['sym']:<3}  {tr['direction']:<5}  "
              f"{tr['entry_dt'].strftime('%Y-%m-%d %H:%M'):16}  "
              f"{tr['exit_dt'].strftime('%Y-%m-%d %H:%M'):16}  "
              f"{tr['hold_h']:4d}  {tr['mae_pct']:7.2f}  {tr['mfe_pct']:7.2f}  {tr['roi_pct']:7.2f}  {tr['reason']}")

    if trades:
        maes = [t["mae_pct"] for t in trades]
        mfes = [t["mfe_pct"] for t in trades]
        rois = [t["roi_pct"] for t in trades]
        print("\nSummary:")
        print(f"Trades: {len(trades)}")
        print(f"Avg MAE: {mean(maes):.2f}% | Med MAE: {median(maes):.2f}%")
        print(f"Avg MFE: {mean(mfes):.2f}% | Med MFE: {median(mfes):.2f}%")
        print(f"Avg ROI: {mean(rois):.2f}% | Med ROI: {median(rois):.2f}%")

if __name__ == "__main__":
    run()
      
