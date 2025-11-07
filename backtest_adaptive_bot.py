#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
backtest_adaptive_conf_leverage.py

Backtests the adaptive signal bot from 2023-01-01 with:
- Default trigger: "confidence" (heat-level >=90 → SHORT, <=10 → LONG)
- Alternate trigger: "zscore" (|z| >= 2.5)
- One trade open at a time across BTC, ETH, SOL (priority: BTC > ETH > SOL)
- TP: median(MFE) for coin from prior trades (>=5) else fallback
- SL: 3%
- Hold window: 4 days (first-touch exit for TP/SL, SL priority)
- Adaptive TP logic, compounding bankroll
- 💥 Leverage: 10× by default (PnL multiplied by 10)
- 💣 Liquidation mode: if adverse move ≥ 1/leverage (10%), trade = -100% loss

Outputs:
  • trades.csv – full trade list (leveraged pnl, bankroll)
  • equity_curve.csv – equity after each trade
"""

import math
import time
import argparse
from dataclasses import dataclass
from typing import List, Dict, Tuple, Optional
from datetime import date, datetime, timedelta

import requests
import pandas as pd

# ---------------------- Config ----------------------
COINS = [("BTCUSDT", "BTC"), ("ETHUSDT", "ETH"), ("SOLUSDT", "SOL")]
Z_THRESH = 2.5
SL = 0.03
HOLD_BARS = 4
TP_FALLBACK = {"BTC": 0.0227, "ETH": 0.0167, "SOL": 0.0444}
START_BANKROLL = 100.0
START_DATE = date(2023, 1, 1)

BASES = [
    "https://api.binance.com",
    "https://api1.binance.com",
    "https://api2.binance.com",
    "https://api3.binance.com",
    "https://data-api.binance.vision",
]
HEADERS = {"User-Agent": "adaptive-bot-backtest/1.3"}

# ---------------------- Data fetch ----------------------
def binance_daily(symbol: str, start_dt: Optional[date] = None) -> List[Tuple[date, float, float, float, float]]:
    """Return list of (date, open, high, low, close)."""
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
                d = datetime.utcfromtimestamp(close_ts).date()
                if start_dt and d < start_dt:
                    continue
                o = float(k[1]); h = float(k[2]); l = float(k[3]); c = float(k[4])
                rows.append((d, o, h, l, c))
            return rows
        except Exception as e:
            last_err = e
            continue
    raise last_err if last_err else RuntimeError("All Binance bases failed")

# ---------------------- Math helpers ----------------------
def pct_returns(closes: List[float]) -> List[float]:
    return [closes[i] / closes[i - 1] - 1 for i in range(1, len(closes))]

def zscore_series_abs(r: List[float], look: int = 20) -> List[Optional[float]]:
    zs = []
    for i in range(len(r)):
        if i + 1 < look:
            zs.append(None)
            continue
        window = r[i + 1 - look : i + 1]
        mu = sum(window) / len(window)
        var = sum((x - mu) ** 2 for x in window) / len(window)
        sd = math.sqrt(var) if var > 0 else 0.0
        zs.append(abs((r[i] - mu) / sd) if sd > 0 else None)
    return zs

def median(values: List[float]) -> Optional[float]:
    v = sorted([x for x in values if x is not None])
    n = len(v)
    if n == 0:
        return None
    if n % 2 == 1:
        return v[n // 2]
    return (v[n // 2 - 1] + v[n // 2]) / 2.0

# ---------------------- Trade structure ----------------------
@dataclass
class Trade:
    open_date: date
    symbol: str
    coin: str
    direction: str
    entry: float
    tp: float
    sl: float
    close_date: Optional[date] = None
    exit_price: Optional[float] = None
    exit_reason: Optional[str] = None
    pnl_pct: Optional[float] = None
    raw_pnl_pct: Optional[float] = None
    mfe_pct: Optional[float] = None
    bankroll_after: Optional[float] = None

# ---------------------- Helpers ----------------------
def dict_by_date(rows): return {d: (o, h, l, c) for (d, o, h, l, c) in rows}

def daterange(d0: date, d1: date):
    cur = d0
    while cur <= d1:
        yield cur
        cur += timedelta(days=1)

def compute_mfe_since_entry(direction, entry, ohlc_by_date, entry_date, last_date):
    max_high = None
    min_low = None
    for d in daterange(entry_date, last_date):
        tpl = ohlc_by_date.get(d)
        if not tpl:
            continue
        _, h, l, _ = tpl
        max_high = h if max_high is None else max(max_high, h)
        min_low  = l if min_low  is None else min(min_low,  l)
    if max_high is None or min_low is None:
        return 0.0
    if direction == "LONG":
        return max(0.0, (max_high / entry) - 1.0)
    else:
        return max(0.0, (entry / min_low) - 1.0)

def first_touch_exit(direction, entry, tp, sl, ohlc_by_date, entry_date, expiry_date):
    tp_up = entry * (1 + tp)
    sl_down = entry * (1 - sl)
    tp_down = entry * (1 - tp)
    sl_up   = entry * (1 + sl)
    for d in daterange(entry_date, expiry_date):
        tpl = ohlc_by_date.get(d)
        if not tpl:
            continue
        _, h, l, _ = tpl
        if direction == "LONG":
            if l <= sl_down: return d, sl_down, "SL"
            if h >= tp_up: return d, tp_up, "TP"
        else:
            if h >= sl_up: return d, sl_up, "SL"
            if l <= tp_down: return d, tp_down, "TP"
    return None, None, None

def median_mfe_for_coin(sym, past_trades):
    mfes = [t.mfe_pct for t in past_trades if t.mfe_pct is not None]
    if len(mfes) >= 5:
        return float(median(mfes))
    return TP_FALLBACK.get(sym, 0.03)

# ---------------------- Signal builder ----------------------
def build_signal_candidates(market, trigger="confidence"):
    daily = {}
    for symbol, sym in COINS:
        rows = market[sym]
        if len(rows) < 25: continue
        dates = [r[0] for r in rows]
        closes = [r[4] for r in rows]
        r = pct_returns(closes)
        zs_abs = zscore_series_abs(r, 20)

        for i in range(len(zs_abs)):
            z_abs = zs_abs[i]
            if z_abs is None: continue
            today = dates[i+1]
            recent_return = r[i]
            z_signed = z_abs if recent_return > 0 else -z_abs
            level = max(0, min(100, round(50 + 20 * z_signed)))

            if trigger == "confidence":
                if level >= 90:
                    direction = "SHORT"
                elif level <= 10:
                    direction = "LONG"
                else:
                    continue
            else:
                if z_abs >= Z_THRESH:
                    direction = "SHORT" if recent_return > 0 else "LONG"
                else:
                    continue

            entry = closes[i+1]
            daily.setdefault(today, []).append((sym, direction, entry))
    return daily

# ---------------------- Backtest ----------------------
def run_backtest(trigger="confidence", leverage=10.0, liquidate=True):
    market = {}
    for symbol, sym in COINS:
        rows = binance_daily(symbol, start_dt=START_DATE - timedelta(days=60))
        rows = [r for r in rows if r[0] >= START_DATE]
        market[sym] = rows
        time.sleep(0.12)

    ohlc_maps = {sym: dict_by_date(rows) for sym, rows in market.items()}
    raw_candidates = build_signal_candidates(market, trigger=trigger)

    bankroll = START_BANKROLL
    trades = []
    open_trade = None
    history_by_coin = {"BTC": [], "ETH": [], "SOL": []}
    priority = {"BTC": 0, "ETH": 1, "SOL": 2}
    all_days = sorted({d for rows in market.values() for (d,_,_,_,_) in rows})

    def apply_leverage(unlev_pnl):
        nonlocal bankroll
        if liquidate and leverage > 1.0 and unlev_pnl <= -1.0 / leverage:
            lev_pnl = -1.0
        else:
            lev_pnl = unlev_pnl * leverage
        bankroll *= (1.0 + lev_pnl)
        return lev_pnl

    for day in all_days:
        if open_trade:
            sym = open_trade.coin
            ohlc = ohlc_maps[sym]
            entry_date = open_trade.open_date
            expiry_date = entry_date + timedelta(days=HOLD_BARS-1)
            last_check = min(day, expiry_date)
            open_trade.mfe_pct = compute_mfe_since_entry(open_trade.direction, open_trade.entry, ohlc, entry_date, last_check)
            exit_d, exit_px, reason = first_touch_exit(open_trade.direction, open_trade.entry, open_trade.tp, open_trade.sl, ohlc, entry_date, last_check)

            if exit_d:
                if open_trade.direction == "LONG":
                    unlev = (exit_px / open_trade.entry) - 1.0
                else:
                    unlev = (open_trade.entry / exit_px) - 1.0
                lev = apply_leverage(unlev)
                open_trade.close_date, open_trade.exit_price, open_trade.exit_reason = exit_d, exit_px, reason
                open_trade.raw_pnl_pct, open_trade.pnl_pct, open_trade.bankroll_after = unlev, lev, bankroll
                trades.append(open_trade)
                history_by_coin[sym].append(open_trade)
                open_trade = None
            elif day >= expiry_date:
                if expiry_date in ohlc:
                    close_px = ohlc[expiry_date][3]
                else:
                    close_px = next((ohlc[d][3] for d in reversed(list(daterange(entry_date, expiry_date))) if d in ohlc), None)
                if close_px:
                    if open_trade.direction == "LONG":
                        unlev = (close_px / open_trade.entry) - 1.0
                    else:
                        unlev = (open_trade.entry / close_px) - 1.0
                    lev = apply_leverage(unlev)
                    open_trade.close_date, open_trade.exit_price, open_trade.exit_reason = expiry_date, close_px, "TIME"
                    open_trade.raw_pnl_pct, open_trade.pnl_pct, open_trade.bankroll_after = unlev, lev, bankroll
                    trades.append(open_trade)
                    history_by_coin[sym].append(open_trade)
                    open_trade = None

        if not open_trade and day in raw_candidates:
            cands = sorted(raw_candidates[day], key=lambda x: priority.get(x[0], 99))
            sym, direction, entry = cands[0]
            tp = median_mfe_for_coin(sym, history_by_coin[sym])
            open_trade = Trade(open_date=day, symbol=f"{sym}USDT", coin=sym, direction=direction, entry=entry, tp=tp, sl=SL)

    if open_trade:
        sym = open_trade.coin
        ohlc = ohlc_maps[sym]
        last_day = max(ohlc.keys())
        close_px = ohlc[last_day][3]
        if open_trade.direction == "LONG":
            unlev = (close_px / open_trade.entry) - 1.0
        else:
            unlev = (open_trade.entry / close_px) - 1.0
        lev = apply_leverage(unlev)
        open_trade.close_date, open_trade.exit_price, open_trade.exit_reason = last_day, close_px, "TIME_END"
        open_trade.raw_pnl_pct, open_trade.pnl_pct, open_trade.mfe_pct, open_trade.bankroll_after = (
            unlev, lev, compute_mfe_since_entry(open_trade.direction, open_trade.entry, ohlc, open_trade.open_date, last_day), bankroll
        )
        trades.append(open_trade)

    trades_df = pd.DataFrame([{
        "open_date": t.open_date, "close_date": t.close_date, "coin": t.coin,
        "direction": t.direction, "entry": t.entry, "tp_pct": t.tp, "sl_pct": t.sl,
        "exit_price": t.exit_price, "exit_reason": t.exit_reason,
        "raw_pnl_pct": t.raw_pnl_pct, "pnl_pct": t.pnl_pct,
        "mfe_pct": t.mfe_pct, "bankroll_after": t.bankroll_after
    } for t in trades])

    equity_df = pd.DataFrame({"trade_index": range(1, len(trades_df)+1),
                              "bankroll": trades_df["bankroll_after"].values})
    return trades_df, equity_df

# ---------------------- Main ----------------------
def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--trigger", choices=["confidence","zscore"], default="confidence",
                        help="Signal trigger: 'confidence' (90/10) or 'zscore' (|z|>=2.5)")
    parser.add_argument("--leverage", type=float, default=10.0,
                        help="Leverage multiplier (default 10x).")
    parser.add_argument("--liquidate", action="store_true",
                        help="Enable liquidation when adverse move >= 1/leverage.")
    args = parser.parse_args()

    trades_df, equity_df = run_backtest(trigger=args.trigger, leverage=args.leverage, liquidate=args.liquidate)

    trades_df.to_csv("trades.csv", index=False)
    equity_df.to_csv("equity_curve.csv", index=False)

    total_trades = len(trades_df)
    wins = int((trades_df["pnl_pct"] > 0).sum())
    losses = int((trades_df["pnl_pct"] < 0).sum())
    winrate = (wins / total_trades * 100) if total_trades else 0.0
    start = START_BANKROLL
    final = float(equity_df["bankroll"].iloc[-1]) if len(equity_df) else START_BANKROLL
    total_ret = (final / start - 1.0) * 100

    label = "90/10 Confidence" if args.trigger == "confidence" else f"Z-Score (|z|>={Z_THRESH})"
    lev = f"{args.leverage:.1f}x"
    print(f"==== Adaptive Bot Backtest (since {START_DATE}) — {label} — Leverage {lev} ====")
    print(f"Trades: {total_trades} | Wins: {wins} | Losses: {losses} | Winrate: {winrate:.2f}%")
    print(f"Start: ${start:.2f} → Final: ${final:.2f} | Total Return: {total_ret:.2f}%")
    if total_trades:
        print("\nFirst 10 trades:\n", trades_df.head(10).to_string(index=False))
        print("\nLast 10 trades:\n", trades_df.tail(10).to_string(index=False))
    print("\nSaved: trades.csv, equity_curve.csv")

if __name__ == "__main__":
    main()
   
