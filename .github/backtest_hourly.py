#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Backtest: hourly version of the daily model
- Data: Binance 1h klines, last ~6 months
- Lookback: 480 hours (~20 days)
- Trigger: |z| >= 2.5 on 1h returns
- Direction: contrarian (SHORT after up hour, LONG after down hour)
- TP: coin-specific fallback (BTC 2.27%, ETH 1.67%, SOL 4.44%)
- SL: 3% (underlying)
- Time stop: 96 hours (no overlap; portfolio-level)
- Output: trades, win rate, avg gain (% underlying), compounded $100 (1× and 10×)
"""

import math, time, requests
from datetime import datetime, timedelta, timezone

COINS = [("BTCUSDT","BTC"), ("ETHUSDT","ETH"), ("SOLUSDT","SOL")]
TP_FALLBACK = {"BTC": 0.0227, "ETH": 0.0167, "SOL": 0.0444}
SL = 0.03
LOOKBACK_H = 480        # ~20 days
TIME_STOP_H = 96
Z_THRESH = 2.5

BASES = [
    "https://api.binance.com",
    "https://api1.binance.com",
    "https://api2.binance.com",
    "https://api3.binance.com",
    "https://data-api.binance.vision",
]
HDR = {"User-Agent": "hourly-backtest/1.0"}

def fetch_klines(symbol: str, interval: str, start_ms: int, end_ms: int):
    """Fetch klines paginated across multiple bases."""
    out = []
    last_err = None
    # Binance max per request is 1000
    cur = start_ms
    while cur < end_ms:
        got = False
        for base in BASES:
            try:
                url = f"{base}/api/v3/klines"
                params = {"symbol":symbol, "interval":interval, "limit":1000, "startTime":cur, "endTime":end_ms}
                r = requests.get(url, params=params, headers=HDR, timeout=30)
                r.raise_for_status()
                data = r.json()
                if not data:
                    got = True
                    cur = end_ms
                    break
                out.extend(data)
                # advance by last candle closeTime + 1 ms
                cur = int(data[-1][6]) + 1
                got = True
                break
            except Exception as e:
                last_err = e
                continue
        if not got:
            raise last_err if last_err else RuntimeError("All Binance bases failed")
        time.sleep(0.05)
    return out

def to_series(klines):
    """Return lists of (ts, close) from klines."""
    ts = []
    closes = []
    for k in klines:
        close_time_ms = int(k[6])
        close_price = float(k[4])
        ts.append(close_time_ms)
        closes.append(close_price)
    return ts, closes

def pct_returns(closes):
    return [closes[i]/closes[i-1]-1.0 for i in range(1, len(closes))]

def zscores(r, look=480):
    out = [None]*len(r)
    for i in range(len(r)):
        if i+1 < look: continue
        window = r[i+1-look:i+1]
        mu = sum(window)/look
        var = sum((x-mu)**2 for x in window)/look
        sd = var**0.5
        out[i] = abs((r[i]-mu)/sd) if sd>0 else None
    return out

def backtest_coin(symbol, sym, start_ms, end_ms):
    k = fetch_klines(symbol, "1h", start_ms, end_ms)
    if len(k) < LOOKBACK_H + 2:
        return {"sym":sym, "trades":0, "wins":0, "avg_gain":0.0, "equity1x":100.0, "equity10x":100.0}
    ts, closes = to_series(k)
    r = pct_returns(closes)
    z = zscores(r, LOOKBACK_H)

    # Trade simulation
    equity1x = 100.0
    equity10x = 100.0
    trades = 0
    wins = 0
    gains = []
    in_trade_until = None  # timestamp ms when we can trade again (no overlap)
    tp_pct = TP_FALLBACK[sym]

    i = 0
    # r[i] maps to transition (i->i+1), z[i] computed on r[:i]
    # We'll consider the "bar close" at index i+1 as the entry point when z[i] triggers
    while i < len(r):
        if z[i] is not None and z[i] >= Z_THRESH:
            # respect no overlap
            entry_close_index = i+1
            entry_ts = ts[entry_close_index]
            if in_trade_until is not None and entry_ts < in_trade_until:
                i += 1
                continue

            direction = "SHORT" if r[i] > 0 else "LONG"
            entry_px = closes[entry_close_index]
            # Next TIME_STOP_H hours maximum
            exit_index = min(entry_close_index + TIME_STOP_H, len(closes)-1)

            hit = None
            exit_px = closes[exit_index]

            # Walk forward hour by hour to see if TP or SL hits first (close-to-close approx)
            for j in range(entry_close_index+1, exit_index+1):
                move = (closes[j]/entry_px) - 1.0
                fav = move if direction=="LONG" else -move
                if fav >= tp_pct:
                    hit = ("TP", closes[j]); exit_px = closes[j]; break
                if fav <= -SL:
                    hit = ("SL", closes[j]); exit_px = closes[j]; break

            ret_underlying = (exit_px/entry_px - 1.0)
            ret_signed = ret_underlying if direction=="LONG" else -ret_underlying

            # Update equities
            equity1x *= (1.0 + ret_signed)
            equity10x *= (1.0 + 10.0*ret_signed)

            trades += 1
            wins += 1 if ret_signed > 0 else 0
            gains.append(ret_signed*100.0)

            # set no-overlap window until TIME_STOP_H hours from entry close
            in_trade_until = ts[entry_close_index] + TIME_STOP_H*60*60*1000

            # Skip forward to end of the trade window to avoid immediate re-entry
            # (keeps portfolio behavior consistent)
            # But allow next bar after exit_index
            i = max(i+1, exit_index-1)
        i += 1

    avg_gain = sum(gains)/len(gains) if gains else 0.0
    win_rate = (wins/trades*100.0) if trades>0 else 0.0
    return {
        "sym": sym,
        "trades": trades,
        "wins": wins,
        "win_rate": win_rate,
        "avg_gain": avg_gain,
        "equity1x": equity1x,
        "equity10x": equity10x
    }

def main():
    end_dt = datetime.now(timezone.utc)
    start_dt = end_dt - timedelta(days=180)   # ~6 months
    start_ms = int(start_dt.timestamp()*1000)
    end_ms   = int(end_dt.timestamp()*1000)

    results = []
    for symbol, sym in COINS:
        try:
            res = backtest_coin(symbol, sym, start_ms, end_ms)
        except Exception as e:
            print(f"{sym}: data/backtest error: {e}")
            continue
        results.append(res)

    # Pooled metrics (treat as independent, same $100 allocated per coin sequence)
    # Also compute a strict "one-trade-at-a-time" portfolio by merging is non-trivial here; we keep pooled simple.
    total_trades = sum(r["trades"] for r in results)
    total_wins   = sum(r["wins"]   for r in results)
    pooled_win_rate = (total_wins/total_trades*100.0) if total_trades>0 else 0.0

    # Print per-coin and pooled summary
    print("Hourly Backtest (last ~6 months; 480h lookback, |z|>=2.5, TP fallback, SL 3%, hold 96h, no overlap)")
    for r in results:
        print(f"- {r['sym']}: trades={r['trades']}, win_rate={r['win_rate']:.1f}%, "
              f"avg_gain={r['avg_gain']:.2f}% (underlying), "
              f"equity_1x=${r['equity1x']:.2f}, equity_10x=${r['equity10x']:.2f}")

    print(f"\nPooled: trades={total_trades}, win_rate={pooled_win_rate:.1f}%")
    # For pooled compounded equity, you can consider multiplying equities if you ran them sequentially; here we report per-coin.

if __name__ == "__main__":
    main()
  
