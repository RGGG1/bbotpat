#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Backtest: threshold trend-follow
- Enter when heat >= 55 (short) or <= 45 (long)
- Exit when heat returns inside (45,55) or flips sign
- Entry/exit at daily close (last fully closed candle)
- Compound per-coin from $100
- Report since 2023-01-01 and since 2025-01-01

Uses the same heat/z-score definition as alerts_binance.py
"""

import requests, math
from datetime import datetime, timezone

COINS = [("BTCUSDT","BTC"), ("ETHUSDT","ETH"), ("SOLUSDT","SOL")]
BASES = [
    "https://api.binance.com",
    "https://api1.binance.com",
    "https://api2.binance.com",
    "https://api3.binance.com",
    "https://data-api.binance.vision",
]
HEADERS = {"User-Agent": "bbot-backtest/1.0"}

LOOKBACK = 20
THRESH   = 55  # heat threshold; long <= (100-THRESH), short >= THRESH

START_A = datetime(2023,1,1, tzinfo=timezone.utc)
START_B = datetime(2025,1,1, tzinfo=timezone.utc)

def binance_daily_closed(symbol, limit=1500):
    last_err=None
    for base in BASES:
        try:
            url=f"{base}/api/v3/klines"
            # Only fully closed candles: endTime = today@00:00:00 UTC - 1ms
            utc_now = datetime.utcnow()
            utc_midnight = datetime(utc_now.year, utc_now.month, utc_now.day, tzinfo=timezone.utc)
            end_time_ms = int(utc_midnight.timestamp()*1000) - 1
            r=requests.get(url, params={"symbol":symbol,"interval":"1d","limit":limit,"endTime":end_time_ms},
                           headers=HEADERS, timeout=30)
            r.raise_for_status()
            data=r.json()
            out=[]
            for k in data:
                close_ts = int(k[6])//1000
                close_px = float(k[4])
                out.append((datetime.utcfromtimestamp(close_ts).replace(tzinfo=timezone.utc), close_px))
            return out
        except Exception as e:
            last_err=e
    raise last_err if last_err else RuntimeError("All Binance bases failed")

def pct_returns(closes):
    return [closes[i]/closes[i-1]-1.0 for i in range(1,len(closes))]

def zscore_series(ret, look=20):
    zs=[]
    for i in range(len(ret)):
        if i+1 < look:
            zs.append(None); continue
        w = ret[i+1-look:i+1]
        mu = sum(w)/look
        var = sum((x-mu)**2 for x in w)/look
        sd = math.sqrt(var) if var>0 else 0.0
        zs.append(abs((ret[i]-mu)/sd) if sd>0 else None)
    return zs

def heat_from_ret_and_z(r_i, z_i):
    if z_i is None: return None
    # positive return → push above 50; negative → below 50
    z_signed = z_i if r_i>0 else -z_i
    h = 50 + 20*z_signed
    return max(0, min(100, round(h)))

def run_backtest(symbol, sym, date_cut):
    """Backtest from date_cut → today. Returns (trades, wins, equity, stats list)."""
    rows = binance_daily_closed(symbol)
    rows = [row for row in rows if row[0] >= date_cut]
    if len(rows) < LOOKBACK+2:
        return 0,0,100.0,[]

    dates, closes = zip(*rows)
    closes = list(closes)
    rets = pct_returns(closes)
    zs   = zscore_series(rets, LOOKBACK)

    # Align lengths: rets/zs indexed from 1..N-1 relative to closes
    heats = [None]  # index 0 corresponds to first close (no return)
    for i in range(1, len(closes)):
        heats.append(heat_from_ret_and_z(rets[i-1], zs[i-1]))

    equity = 100.0
    trades = 0
    wins = 0
    open_pos = None  # dict: {dir, entry_idx, entry_px}

    stats=[]  # (entry_date, exit_date, side, entry_px, exit_px, pct)

    def should_enter(h):
        if h is None: return None
        if h >= THRESH: return "SHORT"
        if h <= (100-THRESH): return "LONG"
        return None

    def should_exit(h, dirn):
        if h is None: return False
        # exit when heat drops inside neutral band or flips to opposite extreme
        if 100-THRESH < h < THRESH:  # back inside (45,55)
            return True
        # hard flip:
        if dirn=="LONG" and h >= THRESH: return True
        if dirn=="SHORT" and h <= (100-THRESH): return True
        return False

    # iterate from LOOKBACK+1 (first valid heat) to end-1 (we need a next close to exit)
    for i in range(1, len(closes)):
        h = heats[i]

        if open_pos is None:
            dirn = should_enter(h)
            if dirn:
                open_pos = {"dir":dirn, "entry_idx":i, "entry_px":closes[i]}
        else:
            if should_exit(h, open_pos["dir"]):
                # exit at today's close
                entry_px = open_pos["entry_px"]
                exit_px  = closes[i]
                if open_pos["dir"] == "LONG":
                    pct = exit_px/entry_px - 1.0
                else:  # SHORT
                    pct = entry_px/exit_px - 1.0
                equity *= (1.0 + pct)
                trades += 1
                if pct > 0: wins += 1
                stats.append((dates[open_pos["entry_idx"]], dates[i], open_pos["dir"], entry_px, exit_px, pct))
                open_pos = None

    # If a trade still open, close it at last close for accounting
    if open_pos is not None:
        entry_px = open_pos["entry_px"]
        exit_px  = closes[-1]
        if open_pos["dir"] == "LONG":
            pct = exit_px/entry_px - 1.0
        else:
            pct = entry_px/exit_px - 1.0
        equity *= (1.0 + pct)
        trades += 1
        if pct > 0: wins += 1
        stats.append((dates[open_pos["entry_idx"]], dates[-1], open_pos["dir"], entry_px, exit_px, pct))

    return trades, wins, equity, stats

def pretty_results(title, cutoff_dt):
    print(f"\n=== {title} (from {cutoff_dt.date()} to last closed) ===")
    total_trades=0; total_wins=0
    for symbol, sym in COINS:
        trades, wins, equity, stats = run_backtest(symbol, sym, cutoff_dt)
        winrate = (wins/trades*100.0) if trades>0 else 0.0
        print(f"- {sym}: trades={trades}, win%={winrate:.1f}%, final_equity=${equity:,.2f}")
        total_trades += trades; total_wins += wins
    if total_trades>0:
        print(f"POOLED: trades={total_trades}, win%={total_wins/total_trades*100:.1f}%")
    print("")

def main():
    pretty_results("Compounding per-coin (no leverage)", START_A)
    pretty_results("Compounding per-coin (no leverage)", START_B)

if __name__ == "__main__":
    main()
