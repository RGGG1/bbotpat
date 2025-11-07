#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
backtest_adaptive_bot.py
Backtest for the adaptive signal bot since 2023-01-01

- Data: Binance daily klines via public REST (no API key required)
- Coins: BTCUSDT, ETHUSDT, SOLUSDT
- Signals: 20-day z-score of daily returns, trigger if |z| >= 2.5
- Direction: SHORT if today's return > 0 else LONG
- TP: median(MFE) for that coin from prior closed trades with MFE (>=5), else fallback
- SL: 3%
- Hold window: 4 daily candles (96h). Exit on first touch of TP/SL (SL priority if both inside bar);
  otherwise time-exit at end of HOLD_BARS (close price).
- One trade open at a time across ALL coins. If a trade closes on day D, a new trade
  may open on the same day D (after processing exit) using that day's signals.
- Bankroll: starts at $100. Compounds on each trade: bankroll *= (1 + pnl_pct).
- Outputs:
  * Prints summary + trades
  * Saves CSVs: trades.csv, equity_curve.csv
"""

import math
import time
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
HEADERS = {"User-Agent": "adaptive-bot-backtest/1.0"}

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

def zscore_series(r: List[float], look: int = 20) -> List[Optional[float]]:
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

# ---------------------- Trade structures ----------------------
@dataclass
class Trade:
    open_date: date
    symbol: str
    coin: str
    direction: str  # LONG/SHORT
    entry: float
    tp: float       # decimal (e.g., 0.02)
    sl: float       # decimal
    close_date: Optional[date] = None
    exit_price: Optional[float] = None
    exit_reason: Optional[str] = None  # TP / SL / TIME / TIME_END
    pnl_pct: Optional[float] = None
    mfe_pct: Optional[float] = None
    bankroll_after: Optional[float] = None

# ---------------------- OHLC helpers ----------------------
def dict_by_date(rows: List[Tuple[date, float, float, float, float]]) -> Dict[date, Tuple[float,float,float,float]]:
    return {d: (o,h,l,c) for (d,o,h,l,c) in rows}

def daterange(d0: date, d1: date):
    cur = d0
    while cur <= d1:
        yield cur
        cur += timedelta(days=1)

def compute_mfe_since_entry(direction: str, entry: float, ohlc_by_date, entry_date: date, last_date: date) -> float:
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

def first_touch_exit(direction: str, entry: float, tp: float, sl: float, ohlc_by_date, entry_date: date, expiry_date: date):
    tp_up = entry * (1 + tp)
    sl_down = entry * (1 - sl)
    tp_down = entry * (1 - tp)
    sl_up   = entry * (1 + sl)
    for d in daterange(entry_date, expiry_date):
        tpl = ohlc_by_date.get(d)
        if not tpl:
            continue
        o, h, l, c = tpl
        if direction == "LONG":
            if l <= sl_down:
                return d, sl_down, "SL"
            if h >= tp_up:
                return d, tp_up, "TP"
        else:
            if h >= sl_up:
                return d, sl_up, "SL"
            if l <= tp_down:
                return d, tp_down, "TP"
    return None, None, None

# ---------------------- Backtest ----------------------
def median_mfe_for_coin(sym: str, past_trades: List[Trade]) -> float:
    mfes = [t.mfe_pct for t in past_trades if t.mfe_pct is not None]
    if len(mfes) >= 5:
        return float(median(mfes))
    return TP_FALLBACK.get(sym, 0.03)

def build_signal_candidates(market: Dict[str, List[Tuple[date,float,float,float,float]]]) -> Dict[date, List[Tuple[str,str,float,float]]]:
    """
    For each calendar day, build at most one candidate per coin:
      return dict: day -> list of tuples (coin, direction, entry_price, tp_placeholder)
    """
    daily_candidates: Dict[date, List[Tuple[str,str,float,float]]] = {}
    for symbol, sym in COINS:
        rows = market[sym]
        if len(rows) < 25:
            continue
        dates = [r[0] for r in rows]
        closes = [r[4] for r in rows]
        r = pct_returns(list(closes))
        zs = zscore_series(r, 20)
        for i in range(len(zs)):
            if zs[i] is None:
                continue
            z = zs[i]
            today = dates[i+1]  # r and zs are shifted by 1 from closes/dates
            recent_return = r[i]
            if z >= Z_THRESH:
                direction = "SHORT" if recent_return > 0 else "LONG"
                entry = closes[i+1]
                daily_candidates.setdefault(today, []).append((sym, direction, entry, None))  # tp decided at open
    return daily_candidates

def run_backtest():
    # 1) Fetch data
    market = {}
    for symbol, sym in COINS:
        rows = binance_daily(symbol, start_dt=START_DATE - timedelta(days=60))  # include warmup
        rows = [r for r in rows if r[0] >= START_DATE]  # keep only START_DATE+
        market[sym] = rows
        time.sleep(0.15)

    # 2) Precompute OHLC maps
    ohlc_maps = {sym: dict_by_date(rows) for sym, rows in market.items()}

    # 3) Precompute raw signal dates per coin/day (tp will be injected adaptively at open time)
    raw_candidates = build_signal_candidates(market)

    # 4) Backtest loop
    bankroll = START_BANKROLL
    trades: List[Trade] = []
    open_trade: Optional[Trade] = None
    history_by_coin: Dict[str, List[Trade]] = {"BTC": [], "ETH": [], "SOL": []}

    priority = {"BTC": 0, "ETH": 1, "SOL": 2}
    all_days = sorted({d for rows in market.values() for (d,_,_,_,_) in rows})

    for day in all_days:
        # Close/update open trade first
        if open_trade:
            sym = open_trade.coin
            ohlc = ohlc_maps[sym]
            entry_date = open_trade.open_date
            expiry_date = entry_date + timedelta(days=HOLD_BARS-1)
            last_check_day = min(day, expiry_date)
            # Update MFE
            mfe = compute_mfe_since_entry(open_trade.direction, open_trade.entry, ohlc, entry_date, last_check_day)
            open_trade.mfe_pct = mfe
            # Check TP/SL
            exit_d, exit_px, reason = first_touch_exit(open_trade.direction, open_trade.entry,
                                                       open_trade.tp, open_trade.sl, ohlc,
                                                       entry_date, last_check_day)
            if exit_d is not None:
                open_trade.close_date = exit_d
                open_trade.exit_price = exit_px
                open_trade.exit_reason = reason
                pnl = (exit_px / open_trade.entry - 1.0) if open_trade.direction == "LONG" else (open_trade.entry / exit_px - 1.0)
                open_trade.pnl_pct = pnl
                bankroll *= (1.0 + pnl)
                open_trade.bankroll_after = bankroll
                trades.append(open_trade)
                history_by_coin[open_trade.coin].append(open_trade)
                open_trade = None
            else:
                # Time-exit on expiry
                if day >= expiry_date:
                    close_px = None
                    if expiry_date in ohlc:
                        close_px = ohlc[expiry_date][3]
                    else:
                        d = expiry_date
                        while close_px is None and d >= entry_date:
                            if d in ohlc:
                                close_px = ohlc[d][3]
                                break
                            d -= timedelta(days=1)
                    if close_px is not None:
                        open_trade.close_date = expiry_date
                        open_trade.exit_price = close_px
                        open_trade.exit_reason = "TIME"
                        pnl = (close_px / open_trade.entry - 1.0) if open_trade.direction == "LONG" else (open_trade.entry / close_px - 1.0)
                        open_trade.pnl_pct = pnl
                        bankroll *= (1.0 + pnl)
                        open_trade.bankroll_after = bankroll
                        trades.append(open_trade)
                        history_by_coin[open_trade.coin].append(open_trade)
                        open_trade = None

        # After closes, consider opening a new trade on today's close
        if open_trade is None and day in raw_candidates:
            cands = raw_candidates[day]
            cands.sort(key=lambda x: priority.get(x[0], 99))
            sym, direction, entry, _ = cands[0]
            tp = median_mfe_for_coin(sym, history_by_coin[sym])  # adaptive from history
            open_trade = Trade(
                open_date=day,
                symbol=f"{sym}USDT",
                coin=sym,
                direction=direction,
                entry=entry,
                tp=tp,
                sl=SL,
            )

    # If still open at end of series, time-exit on last available close
    if open_trade:
        sym = open_trade.coin
        ohlc = ohlc_maps[sym]
        last_day = max(ohlc.keys())
        close_px = ohlc[last_day][3]
        open_trade.close_date = last_day
        open_trade.exit_price = close_px
        open_trade.exit_reason = "TIME_END"
        pnl = (close_px / open_trade.entry - 1.0) if open_trade.direction == "LONG" else (open_trade.entry / close_px - 1.0)
        open_trade.pnl_pct = pnl
        open_trade.mfe_pct = compute_mfe_since_entry(open_trade.direction, open_trade.entry, ohlc, open_trade.open_date, last_day)
        bankroll *= (1.0 + pnl)
        open_trade.bankroll_after = bankroll
        trades.append(open_trade)

    trades_df = pd.DataFrame([{
        "open_date": t.open_date,
        "close_date": t.close_date,
        "coin": t.coin,
        "direction": t.direction,
        "entry": t.entry,
        "tp_pct": t.tp,
        "sl_pct": t.sl,
        "exit_price": t.exit_price,
        "exit_reason": t.exit_reason,
        "pnl_pct": t.pnl_pct,
        "mfe_pct": t.mfe_pct,
        "bankroll_after": t.bankroll_after
    } for t in trades])

    equity_df = pd.DataFrame({
        "trade_index": list(range(1, len(trades_df)+1)),
        "bankroll": trades_df["bankroll_after"].values
    })

    return trades_df, equity_df

def main():
    trades_df, equity_df = run_backtest()

    trades_df.to_csv("trades.csv", index=False)
    equity_df.to_csv("equity_curve.csv", index=False)

    total_trades = len(trades_df)
    wins = int((trades_df["pnl_pct"] > 0).sum())
    losses = int((trades_df["pnl_pct"] < 0).sum())
    winrate = (wins / total_trades * 100) if total_trades else 0.0
    start = START_BANKROLL
    final = float(equity_df["bankroll"].iloc[-1]) if len(equity_df) else START_BANKROLL
    total_ret = (final / start - 1.0) * 100

    print("==== Adaptive Bot Backtest (since 2023-01-01) ====")
    print(f"Trades: {total_trades} | Wins: {wins} | Losses: {losses} | Winrate: {winrate:.2f}%")
    print(f"Start: ${start:.2f} → Final: ${final:.2f} | Total Return: {total_ret:.2f}%")
    if total_trades:
        print("\nFirst 10 trades:\n", trades_df.head(10).to_string(index=False))
        print("\nLast 10 trades:\n", trades_df.tail(10).to_string(index=False))
    print("\nSaved: trades.csv, equity_curve.csv")

if __name__ == "__main__":
    main()
