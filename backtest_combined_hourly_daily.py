#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
backtest_combined_hourly_daily.py
v1.0 — Combined backtest for:
  • Daily leveraged algo (main)
  • Hourly confidence algo (no leverage, 60% entry, 30% trailing stop)
  • Shared bankroll ($100 start)
  • Priority: main algo overrides low-gear hourly trades
  • Compounding equity, max drawdown, per-period stats
"""

import requests, statistics, math
from datetime import datetime, timedelta, timezone

# ─────────────────────────────
# CONFIG
# ─────────────────────────────
COINS = ["BTCUSDT", "ETHUSDT", "SOLUSDT"]
START_BAL = 100.0
MAIN_CONF = 77
HOURLY_CONF = 60
SL = 0.03
TRAIL = 0.3
HOLD_HRS = 96
LOOKBACK_DAYS = 20

BASES = [
    "https://api.binance.com",
    "https://api1.binance.com",
    "https://api2.binance.com",
    "https://api3.binance.com",
    "https://data-api.binance.vision",
]
HEADERS = {"User-Agent": "combined-bot/1.0 (+github actions)"}


# ─────────────────────────────
# HELPERS
# ─────────────────────────────
def binance_klines(symbol, interval="1h", limit=1500):
    last_err = None
    for base in BASES:
        try:
            url = f"{base}/api/v3/klines"
            r = requests.get(url, params={"symbol": symbol, "interval": interval, "limit": limit}, headers=HEADERS, timeout=30)
            r.raise_for_status()
            data = r.json()
            return [(int(k[6]) // 1000, float(k[4])) for k in data]
        except Exception as e:
            last_err = e
            continue
    raise last_err


def pct_returns(closes):
    return [closes[i] / closes[i - 1] - 1 for i in range(1, len(closes))]


def zscore_series(r, look=20):
    zs = []
    for i in range(len(r)):
        if i + 1 < look:
            zs.append(None)
            continue
        w = r[i + 1 - look : i + 1]
        mu = statistics.mean(w)
        sd = statistics.pstdev(w)
        zs.append(abs((r[i] - mu) / sd) if sd > 0 else None)
    return zs


def heat_score(r, look=20):
    z = zscore_series(r, look)
    res = []
    for i in range(len(z)):
        if z[i] is None:
            res.append(None)
        else:
            sign = 1 if r[i] > 0 else -1
            res.append(max(0, min(100, round(50 + sign * z[i] * 20))))
    return res


def drawdown(eqs):
    peak, dd, maxdd = eqs[0], 0, 0
    for x in eqs:
        if x > peak:
            peak = x
        dd = (peak - x) / peak
        if dd > maxdd:
            maxdd = dd
    return maxdd * 100


# ─────────────────────────────
# CORE BACKTEST
# ─────────────────────────────
def simulate_combined(symbols, start_dt, end_dt):
    print(f"\nSimulating combined algo from {start_dt.date()} to {end_dt.date()}...")
    # load hourly data for all
    prices = {}
    for sym in symbols:
        kl = binance_klines(sym, "1h", 1500 * 5)  # plenty of bars (~7500h)
        closes = [p for (_, p) in kl]
        times = [datetime.fromtimestamp(t, tz=timezone.utc) for (t, _) in kl]
        prices[sym] = (times, closes)

    equity = START_BAL
    eq_hist = [equity]
    in_trade = None
    trade_hist = []

    # flatten hourly timestamps (assuming similar time coverage)
    t_ref = prices[symbols[0]][0]
    for i in range(len(t_ref)):
        now = t_ref[i]
        if now < start_dt or now > end_dt:
            continue

        # compute heats for each coin
        heats = {}
        for sym in symbols:
            _, closes = prices[sym]
            if i < 25:
                continue
            r = pct_returns(closes[: i + 1])
            h = heat_score(r, 20)
            heats[sym] = h[-1]

        # step 1: check main algo triggers (daily)
        if now.hour == 0:  # once daily
            best_main = None
            for sym in symbols:
                h = heats.get(sym)
                if h is None:
                    continue
                if h >= MAIN_CONF or h <= 100 - MAIN_CONF:
                    conf = abs(h - 50) * 2
                    best_main = (sym, h, conf)
                    break

            if best_main:
                sym, h, conf = best_main
                if in_trade:
                    # close any open low-gear trade first
                    pnl = (prices[in_trade["sym"]][1][i] / in_trade["entry"] - 1) * (1 if in_trade["side"] == "LONG" else -1)
                    equity *= (1 + pnl)
                    trade_hist.append(pnl)
                    in_trade = None
                in_trade = {"sym": sym, "side": "LONG" if h <= 100 - MAIN_CONF else "SHORT", "entry": prices[sym][1][i], "lev": 10}
                continue

        # step 2: if not in main trade → allow low-gear hourly algo
        if not in_trade:
            best_low = None
            for sym in symbols:
                h = heats.get(sym)
                if h and (h >= HOURLY_CONF or h <= 100 - HOURLY_CONF):
                    best_low = (sym, h)
                    break
            if best_low:
                sym, h = best_low
                in_trade = {"sym": sym, "side": "LONG" if h <= 100 - HOURLY_CONF else "SHORT", "entry": prices[sym][1][i], "lev": 1}

        # step 3: manage trade
        if in_trade:
            sym, side, entry, lev = in_trade["sym"], in_trade["side"], in_trade["entry"], in_trade["lev"]
            cur = prices[sym][1][i]
            move = (cur / entry - 1) if side == "LONG" else (entry / cur - 1)
            pnl = move * lev
            if pnl <= -SL or pnl <= -0.1:  # safety cutoff
                equity *= (1 + pnl)
                trade_hist.append(pnl)
                in_trade = None
            elif pnl >= 0.05 * lev:  # trailing stop
                eq_trail = 0.05 * lev * (1 - TRAIL)
                if pnl < eq_trail:
                    equity *= (1 + pnl)
                    trade_hist.append(pnl)
                    in_trade = None

        eq_hist.append(equity)

    win_rate = sum(1 for x in trade_hist if x > 0) / len(trade_hist) * 100 if trade_hist else 0
    maxdd = drawdown(eq_hist)
    return equity, len(trade_hist), win_rate, maxdd


# ─────────────────────────────
# RUN
# ─────────────────────────────
if __name__ == "__main__":
    now = datetime.now(timezone.utc)
    y2023 = datetime(2023, 1, 1, tzinfo=timezone.utc)
    y2025 = datetime(2025, 1, 1, tzinfo=timezone.utc)

    eq23, n23, w23, dd23 = simulate_combined(COINS, y2023, now)
    print(f"=== Combined Algo (2023-01-01) ===\nTrades: {n23}, Win%: {w23:.1f}, Final$: {eq23:.2f}, MaxDD: {dd23:.1f}%")

    eq25, n25, w25, dd25 = simulate_combined(COINS, y2025, now)
    print(f"=== Combined Algo (2025-01-01) ===\nTrades: {n25}, Win%: {w25:.1f}, Final$: {eq25:.2f}, MaxDD: {dd25:.1f}%")
