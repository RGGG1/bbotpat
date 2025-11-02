#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Hourly confidence backtest — 30% trailing stop (BTC/ETH/SOL)
Adaptive, no fixed TP. Exits when:
- Profit retraces more than 30% from max gain,
- Confidence drops <60%,
- SL = -3% underlying,
- or 96h max hold.
"""

import requests, math, time
from datetime import datetime, timezone

# ────────────────────────────────────────────────
# CONFIG
# ────────────────────────────────────────────────
SYMBOLS = [("BTCUSDT","BTC"), ("ETHUSDT","ETH"), ("SOLUSDT","SOL")]
LOOKBACK_H = 24 * 20         # 480h ≈ 20 days
CONF_ENTER = 60
CONF_STANDARD = 77
SL = 0.03                    # 3% stop
HOLD_BARS = 96               # 96h
TRAIL_FRACTION = 0.30        # close if drop >30% from peak
PYR_STEP = 5
BASE_LEV_STD = 10
MAX_LEV_STD = 14

BASES = [
    "https://api.binance.com",
    "https://api1.binance.com",
    "https://api2.binance.com",
    "https://api3.binance.com",
    "https://data-api.binance.vision",
]
HEADERS = {"User-Agent": "hourly-conf60-trailing/1.0 (+github actions)"}

# ────────────────────────────────────────────────
# Binance helpers
# ────────────────────────────────────────────────
def binance_hourly_full(symbol, start_date):
    start_ms = int(start_date.timestamp() * 1000)
    out = []
    last_err = None
    while True:
        got = False
        for base in BASES:
            try:
                url = f"{base}/api/v3/klines"
                r = requests.get(url, params={
                    "symbol": symbol, "interval": "1h", "limit": 1500, "startTime": start_ms
                }, headers=HEADERS, timeout=30)
                r.raise_for_status()
                part = r.json()
                if not part:
                    return out
                out.extend(part)
                next_ms = int(part[-1][6])
                start_ms = next_ms + 1
                got = True
                break
            except Exception as e:
                last_err = e
                time.sleep(0.25)
                continue
        if not got:
            if out:
                return out
            raise last_err if last_err else RuntimeError("All Binance bases failed")
        if len(part) < 1500:
            return out

def parse_series_hourly(rows):
    dts, closes = [], []
    for k in rows:
        close_ts = int(k[6]) // 1000
        dts.append(datetime.utcfromtimestamp(close_ts).replace(tzinfo=timezone.utc))
        closes.append(float(k[4]))
    return dts, closes

# ────────────────────────────────────────────────
# Analytics
# ────────────────────────────────────────────────
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
    if conf < CONF_ENTER:
        return 0
    if conf >= CONF_STANDARD:
        return BASE_LEV_STD
    frac = (conf - CONF_ENTER) / (CONF_STANDARD - CONF_ENTER)
    lev = 1 + frac * 9
    return int(max(1, min(10, math.floor(lev + 1e-9))))

# ────────────────────────────────────────────────
# Simulation core
# ────────────────────────────────────────────────
def simulate_token(symbol, sym, start_dt, leverage_mode="scaled"):
    rows = binance_hourly_full(symbol, start_dt)
    if not rows or len(rows) < LOOKBACK_H + 3:
        return {"sym": sym, "trades": 0, "wins": 0, "equity": 100.0, "winrate": 0.0, "maxdd": 0.0}

    dts, closes = parse_series_hourly(rows)
    rets = pct_returns(closes)
    zs = zscores(rets, LOOKBACK_H)

    heats = [None]
    for i in range(1, len(closes)):
        heats.append(heat_from_ret_and_z(rets[i-1], zs[i-1]))

    bank = 100.0
    peak_equity = bank
    max_dd = 0.0
    in_pos = False
    direction = None
    entry_px = None
    base_conf = None
    lev = 0
    stop_i = None
    peak_profit = 0.0
    trades = wins = 0

    def move_from_entry(px):
        if not in_pos: return 0.0
        return (px / entry_px - 1.0) if direction == "LONG" else (entry_px / px - 1.0)

    for i in range(LOOKBACK_H + 1, len(closes)):
        h = heats[i]
        conf = confidence_level(h) if h is not None else None

        if not in_pos:
            if conf is not None and conf >= CONF_ENTER:
                direction = "SHORT" if h >= 50 else "LONG"
                entry_px = closes[i]
                base_conf = conf
                lev = 1 if leverage_mode == "none" else lev_scaled_from_conf(conf)
                stop_i = i + HOLD_BARS
                in_pos = True
                peak_profit = 0.0
        else:
            px = closes[i]
            mv = move_from_entry(px)
            eff_lev = lev if leverage_mode != "none" else 1
            real_mv = mv * eff_lev

            # update peak profit for trailing
            if real_mv > peak_profit:
                peak_profit = real_mv

            # check trailing stop trigger
            retrace = (peak_profit - real_mv)
            trail_hit = (peak_profit > 0) and (retrace >= TRAIL_FRACTION * peak_profit)

            exit_now = (
                mv <= -SL
                or (conf is not None and conf < CONF_ENTER)
                or trail_hit
                or i >= stop_i
            )

            if exit_now:
                trades += 1
                if real_mv > 0:
                    wins += 1
                bank *= (1 + real_mv)
                peak_equity = max(peak_equity, bank)
                dd = (peak_equity - bank) / peak_equity if peak_equity > 0 else 0
                max_dd = max(max_dd, dd)
                in_pos = False
                direction = None
                entry_px = None
                lev = 0
                base_conf = None
                stop_i = None
                peak_profit = 0.0
            else:
                # pyramiding logic
                if leverage_mode != "none" and conf is not None and conf >= CONF_STANDARD:
                    if direction == "SHORT" and h > base_conf:
                        add = int((h - base_conf) // PYR_STEP)
                        if add > 0:
                            lev = min(lev + add, MAX_LEV_STD)
                            base_conf = h
                    elif direction == "LONG" and h < base_conf:
                        add = int((base_conf - h) // PYR_STEP)
                        if add > 0:
                            lev = min(lev + add, MAX_LEV_STD)
                            base_conf = h

    winrate = (wins / trades * 100.0) if trades > 0 else 0.0
    return {"sym": sym, "trades": trades, "wins": wins, "winrate": winrate,
            "equity": bank, "maxdd": max_dd * 100.0}

# ────────────────────────────────────────────────
# Runner
# ────────────────────────────────────────────────
def run_period(title, start_dt, levmode):
    results = []
    for symbol, sym in SYMBOLS:
        results.append(simulate_token(symbol, sym, start_dt, levmode))
    print(f"\n=== {title} ===")
    print("SYM  Trades  Win%  Final$   MaxDD%")
    for r in results:
        print(f"{r['sym']:3}  {r['trades']:6d}  {r['winrate']:5.1f}%  {r['equity']:10.2f}   {r['maxdd']:6.2f}")

if __name__ == "__main__":
    run_period("Scaled leverage (2023-01-01)", datetime(2023,1,1,tzinfo=timezone.utc), "scaled")
    run_period("Scaled leverage (2025-01-01)", datetime(2025,1,1,tzinfo=timezone.utc), "scaled")
    run_period("No leverage (2023-01-01)", datetime(2023,1,1,tzinfo=timezone.utc), "none")
    run_period("No leverage (2025-01-01)", datetime(2025,1,1,tzinfo=timezone.utc), "none")
