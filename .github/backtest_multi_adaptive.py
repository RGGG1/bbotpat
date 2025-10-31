#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Multi-Token Adaptive Backtest (v2.6 rules, equity fix)
- Daily closed candles only (Binance endTime)
- Heat via 20d z-score of daily returns (0..100; 50 neutral, dir-aware)
- Signal: heat >= 77 → SHORT, heat <= 23 → LONG
- TP: adaptive per-coin (walk-forward median MFE). If <5 samples, fallback 3%.
- SL: 3%
- Max hold: 4 bars (96h)
- Leverage: 10× base; pyramiding +1× per +5% confidence gain (cap 14×)
- Exit-advisory: early exit if weaker repeat lowers TP and move >= new TP
- Compounding: per token, starting equity $100
- Periods: from 2023-01-01; from 2025-01-01

FIX: Per-trade PnL is bounded by stop * leverage, and equity is floored at 0.
"""

import requests, math
from datetime import datetime, timezone, timedelta

TOKENS = [
    ("BTCUSDT","BTC"),
    ("ETHUSDT","ETH"),
    ("SOLUSDT","SOL"),
    ("ADAUSDT","ADA"),
    ("LINKUSDT","LINK"),
    ("TONUSDT","TON"),
    ("BNBUSDT","BNB"),
    ("XRPUSDT","XRP"),
    ("TRXUSDT","TRX"),
    ("DOGEUSDT","DOGE"),
    ("XLMUSDT","XLM"),
    ("SUIUSDT","SUI"),
    ("LTCUSDT","LTC"),
]

BASES = [
    "https://api.binance.com",
    "https://api1.binance.com",
    "https://api2.binance.com",
    "https://api3.binance.com",
    "https://data-api.binance.vision",
]
HEADERS = {"User-Agent": "bbot-multi-backtest/1.1"}

LOOKBACK = 20
CONF_TRIGGER = 77
SL = 0.03                 # stop (underlying)
HOLD_BARS = 4             # 96h
BASE_LEV = 10
MAX_LEV  = 14
CONF_PER_LEV = 5          # +1× per +5% confidence gain
TP_FALLBACK_DEFAULT = 0.03

START_A = datetime(2023,1,1, tzinfo=timezone.utc)
START_B = datetime(2025,1,1, tzinfo=timezone.utc)

# ---------- Data ----------
def binance_daily_closed(symbol, limit=1500):
    last_err=None
    for base in BASES:
        try:
            url=f"{base}/api/v3/klines"
            now = datetime.utcnow()
            midnight = datetime(now.year, now.month, now.day, tzinfo=timezone.utc)
            end_ms = int(midnight.timestamp()*1000) - 1  # only fully closed candles
            r = requests.get(url, params={"symbol":symbol,"interval":"1d","limit":limit,"endTime":end_ms},
                             headers=HEADERS, timeout=30)
            r.raise_for_status()
            data = r.json()
            out=[]
            for k in data:
                close_ts = int(k[6])//1000
                close_px = float(k[4])
                out.append((datetime.utcfromtimestamp(close_ts).replace(tzinfo=timezone.utc), close_px))
            return out
        except Exception as e:
            last_err=e; continue
    raise last_err if last_err else RuntimeError("All Binance bases failed")

# ---------- Heat ----------
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
    z_signed = z_i if r_i>0 else -z_i
    h = 50 + 20*z_signed
    return max(0, min(100, round(h)))

# ---------- Helpers ----------
def median(v):
    v = sorted(v)
    n = len(v)
    if n==0: return None
    if n%2: return v[n//2]
    return (v[n//2-1]+v[n//2])/2

def leveraged_dir_from_heat(h):
    if h is None: return None
    if h >= CONF_TRIGGER: return "SHORT"
    if h <= (100-CONF_TRIGGER): return "LONG"
    return None

# ---------- Backtest per token ----------

def backtest_token(symbol, sym, start_dt):
    rows = binance_daily_closed(symbol)
    rows = [row for row in rows if row[0] >= start_dt - timedelta(days=LOOKBACK+2)]
    if len(rows) < LOOKBACK+5:
        return {"sym":sym,"trades":0,"wins":0,"avg_roi":0.0,"equity":100.0}

    dates, closes = zip(*rows)
    dates, closes = list(dates), list(closes)
    rets = pct_returns(closes)
    zs   = zscore_series(rets, LOOKBACK)

    # Heat aligned to close index (None at index 0)
    heats = [None]
    for i in range(1,len(closes)):
        heats.append(heat_from_ret_and_z(rets[i-1], zs[i-1]))

    # Walk-forward adaptive TP memory
    prior_mfes = []
    tp_fallback = TP_FALLBACK_DEFAULT

    equity = 100.0
    trades=0; wins=0; rois=[]

    # first usable index
    i = next((k for k,d in enumerate(dates) if d>=start_dt), LOOKBACK+1)

    in_trade = False
    # these are only meaningful while in_trade is True
    entry_i = None
    entry_px = None
    direction = None
    lev = BASE_LEV
    conf_at_entry = None
    tp = None
    last_allowed_bar = None  # end of the 96h window (index)

    while i < len(closes):
        h = heats[i]

        if not in_trade:
            # only consider new entries when flat
            signal_dir = leveraged_dir_from_heat(h)
            if signal_dir is None:
                i += 1
                continue

            # open new trade
            in_trade = True
            direction = signal_dir
            entry_i = i
            entry_px = closes[i]
            conf_at_entry = h
            lev = BASE_LEV
            # adaptive TP (prior MFEs if we have >=5, else fallback)
            tp = median(prior_mfes) if len(prior_mfes) >= 5 else tp_fallback

            # define max holding window (4 daily bars)
            last_allowed_bar = min(i + HOLD_BARS, len(closes)-1)

            # move to the next bar to start monitoring exits
            i += 1
            continue

        # ----- manage open trade here -----
        # Default: if nothing hits, we’ll time-exit at last_allowed_bar
        exit_now = False
        hit_reason = "TIME"

        # Current move (underlying)
        cur_px = closes[i]
        if direction == "LONG":
            move = cur_px/entry_px - 1.0
        else:
            move = entry_px/cur_px - 1.0

        # Track MFE for learning
        # store best move since entry; we’ll finalize at exit
        # (we’ll keep it in a local closure via dict)
        if "_best_move" not in locals():
            _best_move = 0.0
        _best_move = max(_best_move, move)

        # Recompute heat for pyramiding / exit advisory
        hj = heats[i]
        if hj is not None:
            same_dir = (leveraged_dir_from_heat(hj) == direction)
            in_band  = ((direction=="LONG" and hj <= 100-CONF_TRIGGER) or
                        (direction=="SHORT" and hj >= CONF_TRIGGER))

            # Pyramiding (+1× per +5% confidence gain, max 14×)
            if same_dir and in_band:
                if (direction=="SHORT" and hj > conf_at_entry) or (direction=="LONG" and hj < conf_at_entry):
                    steps = int(abs(hj - conf_at_entry)//CONF_PER_LEV)
                    if steps > 0:
                        lev = min(lev + steps, MAX_LEV)
                        conf_at_entry = hj

            # Exit advisory (weaker + lower TP and already >= new TP)
            new_tp = median(prior_mfes) if len(prior_mfes)>=5 else tp_fallback
            weaker  = (direction=="SHORT" and hj < conf_at_entry) or (direction=="LONG" and hj > conf_at_entry)
            lower_tp = new_tp < tp
            if same_dir and in_band and weaker and lower_tp and move >= new_tp:
                exit_now = True
                hit_reason = "ADVISORY"

        # Hard exits: TP/SL/time
        if not exit_now and move >= tp:
            exit_now = True
            hit_reason = "TP"
        if not exit_now and move <= -SL:
            exit_now = True
            hit_reason = "SL"
        if not exit_now and i >= last_allowed_bar:
            exit_now = True
            hit_reason = "TIME"

        if not exit_now:
            i += 1
            continue

        # ----- close the trade -----
        final_px = closes[i]
        if direction == "LONG":
            final_move = final_px/entry_px - 1.0
        else:
            final_move = entry_px/final_px - 1.0

        # Learn from MFE
        prior_mfes.append(_best_move)
        _best_move = 0.0  # reset

        # Apply bounded compounding (cap loss at SL; apply leverage; floor at 0)
        bounded_move = max(final_move, -SL)
        effective = bounded_move * lev
        equity = max(0.0, equity * (1.0 + effective))

        trades += 1
        if effective > 0: wins += 1
        rois.append(final_move)

        # flat again; only now can we consider a new entry
        in_trade = False
        direction = None
        entry_i = None
        entry_px = None
        conf_at_entry = None
        lev = BASE_LEV
        tp = None
        last_allowed_bar = None

        # move forward to *after* the exit bar
        i += 1

    avg_roi = (sum(rois)/len(rois))*100.0 if rois else 0.0
    return {"sym":sym, "trades":trades, "wins":wins, "avg_roi":avg_roi, "equity":equity}


# ---------- Runner ----------
def run_period(start_dt, title):
    rows=[]
    total_trades=0; total_wins=0
    for symbol, sym in TOKENS:
        res = backtest_token(symbol, sym, start_dt)
        total_trades += res["trades"]; total_wins += res["wins"]
        rows.append(res)

    print(f"\n=== Adaptive Leveraged Backtest — {title} ===")
    print(f"{'SYM':<6} {'Trades':>6} {'Win%':>7} {'Avg ROI%':>10} {'Equity $':>12}")
    for r in rows:
        wr = (r["wins"]/r["trades"]*100.0) if r["trades"]>0 else 0.0
        print(f"{r['sym']:<6} {r['trades']:>6} {wr:>6.1f}% {r['avg_roi']:>9.2f}% {r['equity']:>12.2f}")

    pooled_wr = (total_wins/total_trades*100.0) if total_trades>0 else 0.0
    print(f"{'TOTAL':<6} {total_trades:>6} {pooled_wr:>6.1f}% {'':>9} {'':>12}")

    # ---- Force flush so GitHub logs always show full table ----
    import sys
    sys.stdout.flush()
    

def main():
    run_period(START_A, f"from {START_A.date()} to last closed")
    run_period(START_B, f"from {START_B.date()} to last closed")

if __name__ == "__main__":
    main()
        
