#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
backtest.py
──────────────────────────────────────────────
Runs a backtest of the adaptive Binance algo logic.

Rules:
- Indicators based on daily closes.
- Signal = |zscore(20-day returns)| >= 2.5
- LONG if last daily return < 0; SHORT if > 0.
- One position at a time across BTCUSDT, ETHUSDT, SOLUSDT.
- If signal invalidates, close on that day’s close.
- Can close & reopen on same candle.
- Stop-loss 3% (price-based), 10x leverage.
- No take profit.
──────────────────────────────────────────────
Outputs:
- trades.csv → full trade list
- Console summary of compounded equity (start $100)
──────────────────────────────────────────────
"""

import math
import csv
from datetime import datetime, timezone, timedelta, date
from typing import List, Dict, Tuple, Optional
import requests
import pandas as pd

# ────────────────────────────────────────────────
# Config
# ────────────────────────────────────────────────
Z_THRESH = 2.5
STOP_PCT = 0.03          # 3% price stop
LEVERAGE = 10.0
LOOKBACK = 20
PRIORITY = ["BTCUSDT", "ETHUSDT", "SOLUSDT"]

BASES = [
    "https://api.binance.com",
    "https://api1.binance.com",
    "https://api2.binance.com",
    "https://api3.binance.com",
    "https://data-api.binance.vision",
]
HEADERS = {"User-Agent": "backtest-bot/1.0 (+github)"}


# ────────────────────────────────────────────────
# Helpers
# ────────────────────────────────────────────────
def fetch_klines_1d(symbol: str, start: date, end: date) -> List[Dict]:
    """Fetch 1d klines from Binance between start and end."""
    last_err = None
    out: List[Dict] = []
    start_ts = int(datetime(start.year, start.month, start.day, tzinfo=timezone.utc).timestamp() * 1000)
    end_ts = int(datetime(end.year, end.month, end.day, 23, 59, 59, tzinfo=timezone.utc).timestamp() * 1000)

    for base in BASES:
        try:
            url = f"{base}/api/v3/klines"
            params = {"symbol": symbol, "interval": "1d", "limit": 1000,
                      "startTime": start_ts, "endTime": end_ts}
            r = requests.get(url, params=params, headers=HEADERS, timeout=30)
            r.raise_for_status()
            data = r.json()
            if isinstance(data, dict) and data.get("code"):
                raise RuntimeError(f"Binance error {data.get('code')}: {data.get('msg')}")
            for k in data:
                d = datetime.utcfromtimestamp(int(k[6]) // 1000).date()
                if d < start or d > end:
                    continue
                out.append({
                    "date": d,
                    "open": float(k[1]),
                    "high": float(k[2]),
                    "low": float(k[3]),
                    "close": float(k[4]),
                })
            if out:
                break
        except Exception as e:
            last_err = e
            continue
    if not out and last_err:
        raise last_err
    return out


def pct_returns(closes: List[float]) -> List[Optional[float]]:
    r = [None]
    for i in range(1, len(closes)):
        r.append(closes[i] / closes[i - 1] - 1.0)
    return r


def zscore_series_abs(r: List[Optional[float]], look: int = 20) -> List[Optional[float]]:
    zs: List[Optional[float]] = []
    for i, ri in enumerate(r):
        if ri is None or i + 1 < look:
            zs.append(None)
            continue
        window = [x for x in r[i + 1 - look : i + 1] if x is not None]
        if len(window) < look:
            zs.append(None)
            continue
        mu = sum(window) / len(window)
        var = sum((x - mu) ** 2 for x in window) / len(window)
        sd = math.sqrt(var) if var > 0 else 0.0
        zs.append(abs((ri - mu) / sd) if sd > 0 else None)
    return zs


def build_symbol_series(symbol: str, start: date, end: date) -> Dict[date, Dict]:
    klines = fetch_klines_1d(symbol, start, end)
    closes = [row["close"] for row in klines]
    rets = pct_returns(closes)
    zs = zscore_series_abs(rets, LOOKBACK)

    series: Dict[date, Dict] = {}
    for i, row in enumerate(klines):
        d = row["date"]
        r = rets[i]
        z = zs[i]
        signed_dir = None
        if r is not None and z is not None:
            signed_dir = "SHORT" if r > 0 else "LONG"
        series[d] = {
            "open": row["open"],
            "high": row["high"],
            "low": row["low"],
            "close": row["close"],
            "ret": r,
            "z": z,
            "dir": signed_dir,
            "signal": (z is not None and z >= Z_THRESH),
        }
    return series


def pick_entry(candidates: List[Tuple[str, Dict]]) -> Optional[Tuple[str, str]]:
    """Pick highest-priority valid signal."""
    ranked = sorted([c for c in candidates if c[1]["signal"]], key=lambda t: PRIORITY.index(t[0]))
    if not ranked:
        return None
    sym = ranked[0][0]
    direction = ranked[0][1]["dir"]
    return (sym, direction)


# ────────────────────────────────────────────────
# Backtest core
# ────────────────────────────────────────────────
def backtest(start_str="2023-01-01", end_str=None):
    start = datetime.strptime(start_str, "%Y-%m-%d").date()
    end = datetime.now(timezone.utc).date() if not end_str else datetime.strptime(end_str, "%Y-%m-%d").date()

    sym_data: Dict[str, Dict[date, Dict]] = {s: build_symbol_series(s, start, end) for s in PRIORITY}
    all_dates = sorted(set().union(*[set(sym_data[s].keys()) for s in PRIORITY]))

    equity = 100.0
    trades: List[Dict] = []
    position = None

    for d in all_dates:
        def bar(sym): return sym_data[sym].get(d)

        # Stop check
        stopped_today = False
        if position is not None:
            b = bar(position["symbol"])
            if b:
                if position["side"] == "LONG":
                    stop = position["entry_price"] * (1 - STOP_PCT)
                    if b["low"] <= stop:
                        pnl = (stop - position["entry_price"]) / position["entry_price"]
                        roi = pnl * LEVERAGE
                        equity *= (1 + roi)
                        trades.append({
                            "symbol": position["symbol"], "side": position["side"],
                            "entry_date": position["entry_date"].isoformat(),
                            "entry_price": round(position["entry_price"], 2),
                            "exit_date": d.isoformat(), "exit_price": round(stop, 2),
                            "reason": "STOP",
                            "raw_return": round(pnl * 100, 3),
                            "roi_levered": round(roi * 100, 3),
                            "equity_after": round(equity, 2),
                        })
                        position, stopped_today = None, True
                else:
                    stop = position["entry_price"] * (1 + STOP_PCT)
                    if b["high"] >= stop:
                        pnl = (position["entry_price"] - stop) / position["entry_price"]
                        roi = pnl * LEVERAGE
                        equity *= (1 + roi)
                        trades.append({
                            "symbol": position["symbol"], "side": position["side"],
                            "entry_date": position["entry_date"].isoformat(),
                            "entry_price": round(position["entry_price"], 2),
                            "exit_date": d.isoformat(), "exit_price": round(stop, 2),
                            "reason": "STOP",
                            "raw_return": round(pnl * 100, 3),
                            "roi_levered": round(roi * 100, 3),
                            "equity_after": round(equity, 2),
                        })
                        position, stopped_today = None, True

        # Signal-based exit
        if position is not None and not stopped_today:
            b = bar(position["symbol"])
            if b:
                still_valid = (b["signal"] and b["dir"] == position["side"])
                if not still_valid:
                    if position["side"] == "LONG":
                        pnl = (b["close"] - position["entry_price"]) / position["entry_price"]
                    else:
                        pnl = (position["entry_price"] - b["close"]) / position["entry_price"]
                    roi = pnl * LEVERAGE
                    equity *= (1 + roi)
                    trades.append({
                        "symbol": position["symbol"], "side": position["side"],
                        "entry_date": position["entry_date"].isoformat(),
                        "entry_price": round(position["entry_price"], 2),
                        "exit_date": d.isoformat(), "exit_price": round(b["close"], 2),
                        "reason": "CLOSE_SIGNAL",
                        "raw_return": round(pnl * 100, 3),
                        "roi_levered": round(roi * 100, 3),
                        "equity_after": round(equity, 2),
                    })
                    position = None

        # New entries (if flat)
        if position is None:
            todays = []
            for sym in PRIORITY:
                b = bar(sym)
                if b and b["signal"] and b["dir"]:
                    todays.append((sym, b))
            pick = pick_entry(todays)
            if pick:
                sym, side = pick
                b = bar(sym)
                position = {"symbol": sym, "side": side, "entry_date": d, "entry_price": b["close"]}

    # Force close last open position
    if position:
        last_bar = sym_data[position["symbol"]][all_dates[-1]]
        if position["side"] == "LONG":
            pnl = (last_bar["close"] - position["entry_price"]) / position["entry_price"]
        else:
            pnl = (position["entry_price"] - last_bar["close"]) / position["entry_price"]
        roi = pnl * LEVERAGE
        equity *= (1 + roi)
        trades.append({
            "symbol": position["symbol"], "side": position["side"],
            "entry_date": position["entry_date"].isoformat(),
            "entry_price": round(position["entry_price"], 2),
            "exit_date": all_dates[-1].isoformat(),
            "exit_price": round(last_bar["close"], 2),
            "reason": "FORCED_LAST_DAY",
            "raw_return": round(pnl * 100, 3),
            "roi_levered": round(roi * 100, 3),
            "equity_after": round(equity, 2),
        })

    # ────────────────────────────────────────────────
    # Save & report
    # ────────────────────────────────────────────────
    df = pd.DataFrame(trades)
    df.to_csv("trades.csv", index=False)
    print(df.tail(10).to_string(index=False))
    print(f"\nTotal trades: {len(df)}")
    print(f"Final equity: ${equity:.2f} (start $100.00)")
    print("Full trade list saved to trades.csv")


if __name__ == "__main__":
    backtest("2023-01-01")
