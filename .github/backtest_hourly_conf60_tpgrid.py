#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Hourly confidence backtest — TP grid (BTC/ETH/SOL)
Runs two fixed TPs: +10% and +7% underlying.

Rules:
- Enter when confidence >= 60% (either side) on 1h bars (20-day equiv lookback = 480h)
- Leverage scales 1×→10× as confidence rises 60%→77%; at >=77% allow pyramiding +1×/5% up to 14×
- Exit at TP (10% or 7% underlying), or SL -3% underlying, or confidence <60%, or after 96h
- Per-token compounding from $100
- Also runs a no-leverage (1×) variant
"""

import requests, math, time
from datetime import datetime, timezone

SYMBOLS = [("BTCUSDT","BTC"), ("ETHUSDT","ETH"), ("SOLUSDT","SOL")]

LOOKBACK_H = 24*20        # 480h ≈ 20 days
CONF_ENTER = 60           # enter when confidence >=60
CONF_STANDARD = 77        # pyramiding enabled in this regime
SL = 0.03                 # 3% underlying stop
TP_GRID = [0.10, 0.07]    # TEST BOTH: 10% and 7% underlying TP
HOLD_BARS = 96            # 96h = 4 days
PYR_STEP = 5              # +1x per +5% confidence (>=77% only)
BASE_LEV_STD = 10
MAX_LEV_STD  = 14

BASES = [
    "https://api.binance.com",
    "https://api1.binance.com",
    "https://api2.binance.com",
    "https://api3.binance.com",
    "https://data-api.binance.vision",
]
HEADERS = {"User-Agent": "hourly-conf60-tpgrid/1.0 (+github actions)"}

def binance_hourly_full(symbol, start_date):
    """Fetch full 1h klines from start_date (UTC) to now, paging via startTime."""
    start_ms = int(start_date.timestamp()*1000)
    out = []
    last_err = None
    while True:
        got = False
        for base in BASES:
            try:
                url = f"{base}/api/v3/klines"
                r = requests.get(url, params={
                    "symbol":symbol, "interval":"1h", "limit":1500, "startTime":start_ms
                }, headers=HEADERS, timeout=30)
                r.raise_for_status()
                part = r.json()
                if not part:
                    return out
                out.extend(part)
                next_ms = int(part[-1][6])  # close time ms of last bar
                start_ms = next_ms + 1
                got = True
                break
            except Exception as e:
                last_err = e
                time.sleep(0.2)
                continue
        if not got:
            if out: return out
            raise last_err if last_err else RuntimeError("All Binance bases failed")
        if len(part) < 1500:
            return out

def parse_series_hourly(rows):
    dts, closes = [], []
    for k in rows:
        close_ts = int(k[6])//1000
        dts.append(datetime.utcfromtimestamp(close_ts).replace(tzinfo=timezone.utc))
        closes.append(float(k[4]))
    return dts, closes

def pct_returns(cl):
    return [cl[i]/cl[i-1]-1 for i in range(1,len(cl))]

def zscores(r, look=LOOKBACK_H):
    zs=[None]*len(r)
    for i in range(look-1, len(r)):
        w = r[i-(look-1):i+1]
        mu = sum(w)/look
        sd = (sum((x-mu)**2 for x in w)/look)**0.5
        zs[i] = (r[i]-mu)/sd if sd>0 else None
    return zs

def heat_from_ret_and_z(ret, z):
    if z is None: return None
    signed = z if ret>0 else -z
    lvl = 50 + 20*signed
    return max(0, min(100, round(lvl)))

def confidence_level(heat):
    if heat is None: return None
    return max(heat, 100-heat)

def lev_scaled_from_conf(conf):
    """Map 60%→1×, 77%→10× (clamped integer), default 10× at >=77%."""
    if conf < CONF_ENTER:
        return 0
    if conf >= CONF_STANDARD:
        return BASE_LEV_STD
    frac = (conf - CONF_ENTER) / (CONF_STANDARD - CONF_ENTER)  # 0..1
    lev = 1 + frac * 9
    return int(max(1, min(10, math.floor(lev + 1e-9))))

def simulate_token(symbol, sym, start_dt, tp_underlying, leverage_mode="scaled"):
    """
    Per-token bankroll starting $100.
    leverage_mode: "scaled" (as spec) or "none" (always 1×, no pyramiding)
    """
    rows = binance_hourly_full(symbol, start_dt)
    if not rows or len(rows) < LOOKBACK_H+3:
        return {"sym":sym,"trades":0,"wins":0,"equity":100.0,"winrate":0.0}

    dts, closes = parse_series_hourly(rows)
    rets = pct_returns(closes)
    zs   = zscores(rets, LOOKBACK_H)

    heats = [None]
    for i in range(1,len(closes)):
        heats.append(heat_from_ret_and_z(rets[i-1], zs[i-1]))

    bank = 100.0
    in_pos = False
    direction = None
    entry_i = None
    entry_px = None
    base_conf = None
    lev = 0
    stop_i = None
    trades = wins = 0

    i = LOOKBACK_H+1
    while i < len(closes):
        h = heats[i]
        conf = confidence_level(h) if h is not None else None

        def move_from_entry(idx):
            if not in_pos: return 0.0
            px = closes[idx]
            if direction=="LONG":   return px/entry_px - 1.0
            else:                   return entry_px/px - 1.0

        if not in_pos:
            if conf is not None and conf >= CONF_ENTER:
                direction = "SHORT" if h >= 50 else "LONG"
                entry_i = i
                entry_px = closes[i]
                base_conf = conf
                if leverage_mode == "none":
                    lev = 1
                else:
                    lev = lev_scaled_from_conf(conf)  # 1..10 below 77; 10 at >=77
                stop_i = i + HOLD_BARS
                in_pos = True
        else:
            mv = move_from_entry(i)

            # Exit priority: SL, TP, <60%, max hold
            if mv <= -SL:
                eff = (mv) * (lev if leverage_mode!="none" else 1)
                bank = max(0.0, bank * (1.0 + eff))
                trades += 1
                in_pos=False; direction=None; entry_i=None; entry_px=None; base_conf=None; lev=0; stop_i=None
            elif mv >= tp_underlying:
                eff = (tp_underlying) * (lev if leverage_mode!="none" else 1)
                bank = max(0.0, bank * (1.0 + eff))
                trades += 1; wins += 1
                in_pos=False; direction=None; entry_i=None; entry_px=None; base_conf=None; lev=0; stop_i=None
            else:
                if conf is not None and conf < CONF_ENTER:
                    eff = (mv) * (lev if leverage_mode!="none" else 1)
                    bank = max(0.0, bank * (1.0 + eff))
                    trades += 1; 
                    if eff>0: wins+=1
                    in_pos=False; direction=None; entry_i=None; entry_px=None; base_conf=None; lev=0; stop_i=None
                elif i >= stop_i:
                    eff = (mv) * (lev if leverage_mode!="none" else 1)
                    bank = max(0.0, bank * (1.0 + eff))
                    trades += 1; 
                    if eff>0: wins+=1
                    in_pos=False; direction=None; entry_i=None; entry_px=None; base_conf=None; lev=0; stop_i=None
                else:
                    # Pyramiding in standard regime (>=77%) for scaled mode only
                    if leverage_mode != "none" and conf is not None and conf >= CONF_STANDARD:
                        if direction=="SHORT" and h > base_conf:
                            add = int((h - base_conf)//PYR_STEP)
                            if add>0:
                                lev = min(lev + add, MAX_LEV_STD)
                                base_conf = h
                        elif direction=="LONG" and h < base_conf:
                            add = int((base_conf - h)//PYR_STEP)
                            if add>0:
                                lev = min(lev + add, MAX_LEV_STD)
                                base_conf = h

        i += 1

    winrate = (wins/trades*100.0) if trades>0 else 0.0
    return {"sym":sym,"trades":trades,"wins":wins,"winrate":winrate,"equity":bank}

def run_for_tp(tp_underlying, title_suffix):
    def run_range(since_dt, levmode):
        out=[]
        for symbol, sym in SYMBOLS:
            res = simulate_token(symbol, sym, since_dt, tp_underlying, leverage_mode=levmode)
            out.append(res)
        return out

    p1_scaled = run_range(datetime(2023,1,1,tzinfo=timezone.utc), "scaled")
    p2_scaled = run_range(datetime(2025,1,1,tzinfo=timezone.utc), "scaled")
    p1_none   = run_range(datetime(2023,1,1,tzinfo=timezone.utc), "none")
    p2_none   = run_range(datetime(2025,1,1,tzinfo=timezone.utc), "none")

    def show(title, rows):
        print(f"\n=== {title} ===")
        print("SYM   Trades  Win%   FinalEquity$")
        for r in rows:
            print(f"{r['sym']:3}  {r['trades']:6d}  {r['winrate']:5.1f}%  {r['equity']:,.2f}")

    pct = int(tp_underlying*100)
    show(f"Scaled leverage (fixed TP {pct}%) — from 2023-01-01 {title_suffix}", p1_scaled)
    show(f"Scaled leverage (fixed TP {pct}%) — from 2025-01-01 {title_suffix}", p2_scaled)
    show(f"No leverage (fixed TP {pct}%) — from 2023-01-01 {title_suffix}", p1_none)
    show(f"No leverage (fixed TP {pct}%) — from 2025-01-01 {title_suffix}", p2_none)

if __name__ == "__main__":
    for tp in TP_GRID:
        run_for_tp(tp, "")
