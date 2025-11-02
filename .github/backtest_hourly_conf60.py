#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Hourly confidence backtest (BTC/ETH/SOL) with FIXED 5% TP
- Enter when confidence >= 60% (either side) on 1h bars
- Leverage scales 1×→10× as confidence rises from 60%→77%
- At >=77% we still allow pyramiding (+1× per +5%) up to 14×
- Fixed TP: +5% underlying move from entry (equity gain = leverage * 5%)
- Exit early if confidence <60%, SL -3% underlying, or after 96h max hold
- Per-token compounding from $100
- Also runs a no-leverage (1×) variant
- Confidence/heat from 20-day-equivalent lookback = 480 hourly returns
"""

import requests, math, time
from datetime import datetime, timedelta, timezone

SYMBOLS = [("BTCUSDT","BTC"), ("ETHUSDT","ETH"), ("SOLUSDT","SOL")]

LOOKBACK_H = 24*20        # 480h = ~20 days
CONF_ENTER = 60           # enter when confidence >=60
CONF_STANDARD = 77        # pyramiding allowed in "standard" regime
SL = 0.03                 # 3% underlying stop
TP_UNDERLYING = 0.05      # 5% underlying take profit (equity gain = lev * 5%)
HOLD_BARS = 96            # 96h = 4 days
PYR_STEP = 5              # +1x per +5% confidence (standard mode only)
BASE_LEV_STD = 10
MAX_LEV_STD  = 14

BASES = [
    "https://api.binance.com",
    "https://api1.binance.com",
    "https://api2.binance.com",
    "https://api3.binance.com",
    "https://data-api.binance.vision",
]
HEADERS = {"User-Agent": "hourly-conf60-fixedTP5/1.0 (+github actions)"}

def binance_hourly_full(symbol, start_date):
    """Fetch full 1h klines from start_date (UTC) to now, paging via startTime."""
    start_dt = datetime(start_date.year, start_date.month, start_date.day, tzinfo=timezone.utc)
    start_ms = int(start_dt.timestamp()*1000)
    out = []
    last_err = None
    while True:
        got = False
        for base in BASES:
            try:
                url = f"{base}/api/v3/klines"
                r = requests.get(url, params={"symbol":symbol,"interval":"1h","limit":1500,"startTime":start_ms},
                                 headers=HEADERS, timeout=30)
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
    dates, closes = [], []
    for k in rows:
        close_ts = int(k[6])//1000
        dates.append(datetime.utcfromtimestamp(close_ts).replace(tzinfo=timezone.utc))
        closes.append(float(k[4]))
    return dates, closes

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
    """Absolute confidence relative to bands: max(heat, 100-heat)."""
    if heat is None: return None
    return max(heat, 100-heat)

def lev_scaled_from_conf(conf):
    """
    Before standard mode (60..77):
      map 60% → 1×, 77% → 10×, clamp [1,10], integer.
    """
    if conf < CONF_ENTER:
        return 0
    if conf >= CONF_STANDARD:
        return BASE_LEV_STD
    frac = (conf - CONF_ENTER) / (CONF_STANDARD - CONF_ENTER)  # 0..1
    lev = 1 + frac * 9
    return int(max(1, min(10, math.floor(lev + 1e-9))))

def simulate_token(symbol, sym, start_date, leverage_mode="scaled"):
    """
    Simulates one token with per-token bankroll starting at $100.
    leverage_mode: "scaled" (spec above) or "none" (always 1×, no pyramiding)
    """
    rows = binance_hourly_full(symbol, start_date)
    if not rows or len(rows) < LOOKBACK_H+3:
        return {"sym":sym,"trades":0,"wins":0,"equity":100.0,"winrate":0.0}

    dts, closes = parse_series_hourly(rows)
    rets = pct_returns(closes)
    zs   = zscores(rets, LOOKBACK_H)

    heats = [None]  # align with closes; heats[i] uses ret[i-1], zs[i-1]
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
    max_hold = HOLD_BARS

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
            # entry
            if conf is not None and conf >= CONF_ENTER:
                direction = "SHORT" if h >= 50 else "LONG"
                entry_i = i
                entry_px = closes[i]
                base_conf = conf
                if leverage_mode == "none":
                    lev = 1
                else:
                    lev = lev_scaled_from_conf(conf)  # 1..10 below 77; 10 at 77
                stop_i = i + max_hold
                in_pos = True
        else:
            mv = move_from_entry(i)

            # exits in priority order: SL, TP, <60%, max hold
            if mv <= -SL:
                eff = (mv) * (lev if leverage_mode!="none" else 1)
                bank = max(0.0, bank * (1.0 + eff))
                trades += 1; 
                in_pos = False; direction=None; entry_i=None; entry_px=None; base_conf=None; lev=0; stop_i=None
            elif mv >= TP_UNDERLYING:
                eff = (TP_UNDERLYING) * (lev if leverage_mode!="none" else 1)
                bank = max(0.0, bank * (1.0 + eff))
                trades += 1; wins += 1
                in_pos = False; direction=None; entry_i=None; entry_px=None; base_conf=None; lev=0; stop_i=None
            elif conf is not None and conf < CONF_ENTER:
                eff = (mv) * (lev if leverage_mode!="none" else 1)
                bank = max(0.0, bank * (1.0 + eff))
                trades += 1; 
                if eff>0: wins+=1
                in_pos = False; direction=None; entry_i=None; entry_px=None; base_conf=None; lev=0; stop_i=None
            elif i >= stop_i:
                eff = (mv) * (lev if leverage_mode!="none" else 1)
                bank = max(0.0, bank * (1.0 + eff))
                trades += 1; 
                if eff>0: wins+=1
                in_pos = False; direction=None; entry_i=None; entry_px=None; base_conf=None; lev=0; stop_i=None
            else:
                # pyramiding only in standard regime (>=77%) and only in scaled mode
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

def run_periods():
    def run_range(since_date, levmode):
        out=[]
        for symbol, sym in SYMBOLS:
            res = simulate_token(symbol, sym, since_date, leverage_mode=levmode)
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

    show("Scaled leverage (fixed TP 5%) — from 2023-01-01", p1_scaled)
    show("Scaled leverage (fixed TP 5%) — from 2025-01-01", p2_scaled)
    show("No leverage (fixed TP 5%) — from 2023-01-01", p1_none)
    show("No leverage (fixed TP 5%) — from 2025-01-01", p2_none)

if __name__ == "__main__":
    run_periods()
