#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Hourly confidence backtest (BTC/ETH/SOL)
- Enter when confidence >= 60% (direction from heat side)
- Scale lev 1×→10× as confidence 60→77; at >=77% use "standard" rules:
    * base 10×, pyramiding +1×/5% up to 14×
    * SL 3%, adaptive TP (walk-forward median MFE, fallback)
    * Max hold 96h
- Exit when confidence < 60%
- Per-token compounding from $100
- Also runs no-leverage (1×) mode
- Confidence/heat from 20-day-equivalent lookback = 480 hourly returns
"""

import requests, math, time
from datetime import datetime, timedelta, timezone

SYMBOLS = [("BTCUSDT","BTC"), ("ETHUSDT","ETH"), ("SOLUSDT","SOL")]

LOOKBACK_H = 24*20        # 480h = 20 days
CONF_ENTER = 60           # enter when confidence >=60
CONF_STANDARD = 77        # switch to standard mode at >=77
SL = 0.03                 # 3% underlying stop
HOLD_BARS = 96            # 96 hourly bars = 4 days
PYR_STEP = 5              # +1x per +5% confidence (standard mode only)
BASE_LEV_STD = 10
MAX_LEV_STD  = 14

# fallback TP (same as your daily setup)
TP_FALLBACK = {"BTC":0.0227, "ETH":0.0167, "SOL":0.0444}

BASES = [
    "https://api.binance.com",
    "https://api1.binance.com",
    "https://api2.binance.com",
    "https://api3.binance.com",
    "https://data-api.binance.vision",
]
HEADERS = {"User-Agent": "hourly-conf60-backtest/1.0 (+github actions)"}

def binance_hourly_full(symbol, start_date):
    """Fetch full 1h klines from start_date (UTC) to now, paging with startTime."""
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
                # next page
                next_ms = int(part[-1][6])    # close time ms of last bar
                # step ahead 1 ms to avoid repeating last item
                start_ms = next_ms + 1
                got = True
                break
            except Exception as e:
                last_err = e
                time.sleep(0.2)
                continue
        if not got:
            # no endpoint succeeded; bail with what we have
            if out:
                return out
            raise last_err if last_err else RuntimeError("All Binance bases failed")
        # If fewer than 1500 returned, likely finished
        if len(part) < 1500:
            return out

def parse_series_hourly(rows):
    dates = []
    closes = []
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
    # signed: positive for up, negative for down
    signed = z if ret>0 else -z
    lvl = 50 + 20*signed
    return max(0, min(100, round(lvl)))

def confidence_level(heat):
    """Absolute confidence w.r.t. band: max(heat, 100-heat)."""
    if heat is None: return None
    return max(heat, 100-heat)

def tp_adaptive(sym, prior_mfes):
    arr = prior_mfes.get(sym, [])
    if len(arr) >= 5:
        v = sorted(arr)
        n = len(v)
        return v[n//2] if n%2 else (v[n//2-1]+v[n//2])/2.0
    return TP_FALLBACK.get(sym, 0.03)

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
    leverage_mode: "scaled" (as spec) or "none" (always 1×, no pyramiding)
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

    # track prior MFEs for adaptive TP in standard mode
    prior_mfes = {sym:[]}

    i = LOOKBACK_H+1
    while i < len(closes):
        h = heats[i]
        conf = confidence_level(h) if h is not None else None

        def current_move(i_idx):
            if not in_pos: return 0.0
            px = closes[i_idx]
            if direction=="LONG":   return px/entry_px - 1.0
            else:                   return entry_px/px - 1.0

        if not in_pos:
            # entry rule
            if conf is not None and conf >= CONF_ENTER:
                # decide direction by band side
                direction = "SHORT" if h >= 50 else "LONG"
                entry_i = i
                entry_px = closes[i]
                base_conf = conf
                # leverage
                if leverage_mode == "none":
                    lev = 1
                else:
                    lev = lev_scaled_from_conf(conf)  # 1..10 before standard
                stop_i = i + max_hold
                in_pos = True
        else:
            # exit conditions common: confidence < 60% or SL or max hold
            mv = current_move(i)
            if mv <= -SL or i >= stop_i or (conf is not None and conf < CONF_ENTER):
                # record MFE for adaptive TP learning
                # compute MFE since entry (max favorable move)
                best = 0.0
                for j in range(entry_i, i+1):
                    m = (closes[j]/entry_px - 1.0) if direction=="LONG" else (entry_px/closes[j] - 1.0)
                    if m > best: best = m
                prior_mfes[sym].append(best)

                # settle PnL
                bounded = max(mv, -SL)
                eff = bounded * (lev if leverage_mode!="none" else 1)
                bank = max(0.0, bank * (1.0 + eff))
                trades += 1
                if eff > 0: wins += 1

                # flat
                in_pos = False
                direction = None
                entry_i = None
                entry_px = None
                base_conf = None
                lev = 0
                stop_i = None
            else:
                # if in standard mode (>=77%), use TP & pyramiding rules
                if conf is not None and conf >= CONF_STANDARD and leverage_mode != "none":
                    # pyramiding
                    if direction=="SHORT" and h > base_conf:
                        add = int((h - base_conf)//PYR_STEP)
                        if add > 0:
                            lev = min(lev + add, MAX_LEV_STD)
                            base_conf = h
                    elif direction=="LONG" and h < base_conf:
                        add = int((base_conf - h)//PYR_STEP)
                        if add > 0:
                            lev = min(lev + add, MAX_LEV_STD)
                            base_conf = h

                    # adaptive TP
                    tp = tp_adaptive(sym, prior_mfes)
                    if mv >= tp:
                        # take profit
                        bounded = mv
                        eff = bounded * lev
                        bank = max(0.0, bank * (1.0 + eff))
                        trades += 1
                        if eff > 0: wins += 1
                        in_pos = False
                        direction=None; entry_i=None; entry_px=None; base_conf=None; lev=0; stop_i=None

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

    show("Scaled leverage (1×→10× pre-77%; std mode ≥77%) — from 2023-01-01", p1_scaled)
    show("Scaled leverage (1×→10× pre-77%; std mode ≥77%) — from 2025-01-01", p2_scaled)
    show("No leverage (1× only) — from 2023-01-01", p1_none)
    show("No leverage (1× only) — from 2025-01-01", p2_none)

if __name__ == "__main__":
    run_periods()
