#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Backtest: hourly version of the daily model (GLOBAL portfolio)
- Data: Binance 1h klines, last ~6 months
- Lookback: 480 hours (~20 days)
- Trigger: |z| >= 2.5 on 1h returns
- Direction: contrarian (SHORT after up hour, LONG after down hour)
- TP: coin-specific fallback (BTC 2.27%, ETH 1.67%, SOL 4.44%)
- SL: 3% (underlying)
- Time stop: 96 hours
- No overlap: enforced GLOBALLY across all coins (one position at a time)
- Output:
    per-coin summary (like before),
    PLUS global portfolio compounded equity from $100 (1x and 10x)
"""

import time, requests
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
HDR = {"User-Agent": "hourly-backtest/1.1"}

def fetch_klines(symbol: str, interval: str, start_ms: int, end_ms: int):
    out = []
    last_err = None
    cur = start_ms
    while cur < end_ms:
        got = False
        for base in BASES:
            try:
                url = f"{base}/api/v3/klines"
                params = {"symbol":symbol, "interval":interval, "limit":1000,
                          "startTime":cur, "endTime":end_ms}
                r = requests.get(url, params=params, headers=HDR, timeout=30)
                r.raise_for_status()
                data = r.json()
                if not data:
                    got = True
                    cur = end_ms
                    break
                out.extend(data)
                cur = int(data[-1][6]) + 1  # next after last closeTime
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
    ts, closes = [], []
    for k in klines:
        ts.append(int(k[6]))       # closeTime (ms)
        closes.append(float(k[4])) # close
    return ts, closes

def pct_returns(closes):
    return [closes[i]/closes[i-1]-1.0 for i in range(1,len(closes))]

def zscores(r, look=480):
    out = [None]*len(r)
    for i in range(len(r)):
        if i+1 < look: continue
        window = r[i+1-look:i+1]
        mu = sum(window)/look
        var = sum((x-mu)**2 for x in window)/look
        sd  = var**0.5
        out[i] = abs((r[i]-mu)/sd) if sd>0 else None
    return out

def build_trades_for_coin(symbol, sym, start_ms, end_ms):
    """
    Returns a list of trade dicts for this coin:
    {sym, entry_ts, exit_ts, ret_signed, tp_hit(bool), sl_hit(bool)}
    Uses coin-level no-overlap while building (to avoid serial stacking),
    then the global portfolio layer enforces cross-coin no-overlap.
    """
    k = fetch_klines(symbol, "1h", start_ms, end_ms)
    if len(k) < LOOKBACK_H + 2:
        return []
    ts, closes = to_series(k)
    r = pct_returns(closes)
    z = zscores(r, LOOKBACK_H)

    trades = []
    in_trade_until = None
    tp_pct = TP_FALLBACK[sym]

    i = 0
    while i < len(r):
        if z[i] is not None and z[i] >= Z_THRESH:
            entry_close_index = i+1
            entry_ts = ts[entry_close_index]
            if in_trade_until is not None and entry_ts < in_trade_until:
                i += 1
                continue

            direction = "SHORT" if r[i] > 0 else "LONG"
            entry_px  = closes[entry_close_index]
            exit_index = min(entry_close_index + TIME_STOP_H, len(closes)-1)
            exit_px   = closes[exit_index]
            tp_hit = sl_hit = False

            for j in range(entry_close_index+1, exit_index+1):
                move = (closes[j]/entry_px) - 1.0
                fav  = move if direction=="LONG" else -move
                if fav >= tp_pct:
                    tp_hit = True; exit_index = j; exit_px = closes[j]; break
                if fav <= -SL:
                    sl_hit = True; exit_index = j; exit_px = closes[j]; break

            ret_underlying = (exit_px/entry_px - 1.0)
            ret_signed = ret_underlying if direction=="LONG" else -ret_underlying

            trades.append({
                "sym": sym,
                "entry_ts": ts[entry_close_index],
                "exit_ts":  ts[exit_index],
                "ret_signed": ret_signed,
                "tp_hit": tp_hit,
                "sl_hit": sl_hit
            })

            in_trade_until = ts[entry_close_index] + TIME_STOP_H*60*60*1000
            i = max(i+1, exit_index-1)
        i += 1

    return trades

def main():
    end_dt = datetime.now(timezone.utc)
    start_dt = end_dt - timedelta(days=180)
    start_ms = int(start_dt.timestamp()*1000)
    end_ms   = int(end_dt.timestamp()*1000)

    # 1) Per-coin stats (unchanged)
    per = []
    for symbol, sym in COINS:
        trades = build_trades_for_coin(symbol, sym, start_ms, end_ms)
        equity1x = equity10x = 100.0
        wins = 0
        gains = []
        for t in trades:
            equity1x  *= (1.0 + t["ret_signed"])
            equity10x *= (1.0 + 10.0*t["ret_signed"])
            wins += 1 if t["ret_signed"] > 0 else 0
            gains.append(t["ret_signed"]*100.0)
        avg_gain = (sum(gains)/len(gains)) if gains else 0.0
        win_rate = (wins/len(trades)*100.0) if trades else 0.0
        per.append({
            "sym": sym, "trades": len(trades), "wins": wins,
            "win_rate": win_rate, "avg_gain": avg_gain,
            "equity1x": equity1x, "equity10x": equity10x,
            "trades_list": trades
        })

    # Print per-coin like before
    print("Hourly Backtest (last ~6 months; 480h lookback, |z|>=2.5, TP fallback, SL 3%, hold 96h)")
    total_trades = total_wins = 0
    for r in per:
        total_trades += r["trades"]; total_wins += r["wins"]
        print(f"- {r['sym']}: trades={r['trades']}, win_rate={r['win_rate']:.1f}%, "
              f"avg_gain={r['avg_gain']:.2f}% (underlying), "
              f"equity_1x=${r['equity1x']:.2f}, equity_10x=${r['equity10x']:.2f}")
    pooled_win = (total_wins/total_trades*100.0) if total_trades else 0.0
    print(f"\nPooled (counts only): trades={total_trades}, win_rate={pooled_win:.1f}%")

    # 2) GLOBAL portfolio (one position at a time across all coins)
    all_trades = []
    for r in per:
        all_trades.extend(r["trades_list"])
    # sort by entry time
    all_trades.sort(key=lambda t: (t["entry_ts"], t["exit_ts"]))

    glob_equity1x = glob_equity10x = 100.0
    glob_trades = 0
    glob_wins = 0
    glob_gains = []
    available_from = -1

    for t in all_trades:
        if t["entry_ts"] >= available_from:
            # take this trade
            ret = t["ret_signed"]
            glob_equity1x  *= (1.0 + ret)
            glob_equity10x *= (1.0 + 10.0*ret)
            glob_trades += 1
            glob_wins   += 1 if ret > 0 else 0
            glob_gains.append(ret*100.0)
            available_from = t["exit_ts"]  # next trade must start after this exit

    glob_avg = (sum(glob_gains)/len(glob_gains)) if glob_gains else 0.0
    glob_wr  = (glob_wins/glob_trades*100.0) if glob_trades else 0.0

    print("\nGLOBAL portfolio (one trade at a time across BTC/ETH/SOL)")
    print(f"Trades used={glob_trades}, win_rate={glob_wr:.1f}%, avg_gain={glob_avg:.2f}%")
    print(f"Compounded from $100 → 1x: ${glob_equity1x:.2f}   10x: ${glob_equity10x:.2f}")

if __name__ == "__main__":
    main()
    
