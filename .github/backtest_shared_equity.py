#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Shared-bankroll backtest with EV tie-break and same-bar re-entry
Coins: BTC, ETH, SOL, ADA, TON, XRP, TRX, SUI, LTC
Excludes: BNB, DOGE, LINK, XLM
Rules: 77% trigger, 3% SL, adaptive TP, 10→14x pyramiding, 96h cap, exit-advisory
One trade at a time, compounding from $100.
"""

import requests, time
from datetime import datetime, timedelta

# ── Config ───────────────────────────────────────────────────────────────────
SYMBOLS = [
    ("BTCUSDT","BTC"), ("ETHUSDT","ETH"), ("SOLUSDT","SOL"),
    ("ADAUSDT","ADA"), ("TONUSDT","TON"), ("XRPUSDT","XRP"),
    ("TRXUSDT","TRX"), ("SUIUSDT","SUI"), ("LTCUSDT","LTC"),
]
LOOKBACK      = 20
CONF_TRIGGER  = 77
SL            = 0.03
HOLD_BARS     = 4           # 96h
BASE_LEV      = 10
MAX_LEV       = 14
CONF_PER_LEV  = 5           # +1x per +5% confidence
TP_FALLBACKS  = {
    "BTC":0.0227, "ETH":0.0167, "SOL":0.0444,
    "ADA":0.0300, "TON":0.0300, "XRP":0.0300,
    "TRX":0.0250, "SUI":0.0400, "LTC":0.0350
}

BASES = [
    "https://api.binance.com", "https://api1.binance.com",
    "https://api2.binance.com", "https://api3.binance.com",
    "https://data-api.binance.vision",
]
HEADERS = {"User-Agent":"shared-equity-ev/1.1 (+github actions)"}

# ── Data helpers ─────────────────────────────────────────────────────────────
def binance_daily_closed(symbol):
    last=None
    for base in BASES:
        try:
            url=f"{base}/api/v3/klines"
            r=requests.get(url, params={"symbol":symbol,"interval":"1d","limit":1500},
                           headers=HEADERS, timeout=30)
            r.raise_for_status()
            data=r.json()
            out=[]
            for k in data:
                d = datetime.utcfromtimestamp(int(k[6])//1000).date()
                out.append((d, float(k[4])))
            return out
        except Exception as e:
            last=e; time.sleep(0.2)
    raise last if last else RuntimeError("All Binance bases failed")

def pct_returns(cl):
    return [cl[i]/cl[i-1]-1 for i in range(1,len(cl))]

def zscore_series(r, look=20):
    zs=[]
    for i in range(len(r)):
        if i+1<look: zs.append(None); continue
        w=r[i+1-look:i+1]; mu=sum(w)/look
        sd=(sum((x-mu)**2 for x in w)/look)**0.5
        zs.append(abs((r[i]-mu)/sd) if sd>0 else None)
    return zs

def heat_from_ret_and_z(ret,z):
    if z is None: return None
    s = z if ret>0 else -z
    return max(0,min(100, round(50 + s*20)))

def dir_from_heat(h):
    if h is None: return None
    if h>=CONF_TRIGGER: return "SHORT"
    if h<=100-CONF_TRIGGER: return "LONG"
    return None

def confidence_from_heat(h, direction):
    # map to 77..100 for both sides at trigger
    return h if direction=="SHORT" else (100 - h)

def median(a):
    v=[x for x in a if x is not None]; v.sort()
    n=len(v); 
    if n==0: return None
    return v[n//2] if n%2 else (v[n//2-1]+v[n//2])/2.0

# ── Build series per token ───────────────────────────────────────────────────
def prep_token(symbol, sym):
    rows = binance_daily_closed(symbol)
    dates, closes = zip(*rows)
    dates, closes = list(dates), list(closes)
    rets = pct_returns(closes)
    zs   = zscore_series(rets, LOOKBACK)
    heats=[None]
    for i in range(1,len(closes)):
        heats.append(heat_from_ret_and_z(rets[i-1], zs[i-1]))
    return {"sym":sym,"dates":dates,"closes":closes,"heats":heats}

def next_trigger_idx(series, start_idx):
    h = series["heats"]; n=len(h)
    for i in range(max(start_idx, LOOKBACK+1), n):
        if dir_from_heat(h[i]) is not None:
            return i
    return None

# ── Simulate one trade on a token from entry index to exit ───────────────────
def simulate_trade(series, entry_i, prior_mfes, equity):
    closes, heats, sym = series["closes"], series["heats"], series["sym"]
    entry_px = closes[entry_i]
    direction = dir_from_heat(heats[entry_i])
    conf0 = heats[entry_i]
    lev   = BASE_LEV
    tp    = median(prior_mfes[sym]) if len(prior_mfes[sym])>=5 else TP_FALLBACKS.get(sym,0.03)
    last_allowed = min(entry_i + HOLD_BARS, len(closes)-1)

    best_move = 0.0
    i = entry_i + 1
    while i < len(closes):
        cur_px = closes[i]
        move = (cur_px/entry_px - 1.0) if direction=="LONG" else (entry_px/cur_px - 1.0)
        if move>best_move: best_move = move

        h = heats[i]
        if h is not None:
            same = (dir_from_heat(h)==direction)
            inband = ((direction=="LONG" and h<=100-CONF_TRIGGER) or
                      (direction=="SHORT" and h>=CONF_TRIGGER))
            if same and inband:
                stronger = (direction=="SHORT" and h>conf0) or (direction=="LONG" and h<conf0)
                if stronger:
                    steps=int(abs(h-conf0)//CONF_PER_LEV)
                    if steps>0:
                        lev=min(lev+steps, MAX_LEV); conf0=h

                new_tp = median(prior_mfes[sym]) if len(prior_mfes[sym])>=5 else TP_FALLBACKS.get(sym,0.03)
                weaker=(direction=="SHORT" and h<conf0) or (direction=="LONG" and h>conf0)
                if weaker and new_tp<tp and move>=new_tp:
                    break  # exit advisory

        if move>=tp: break
        if move<=-SL: break
        if i>=last_allowed: break
        i+=1

    # close at i (same-day close)
    final_px = closes[i]
    final_move = (final_px/entry_px - 1.0) if direction=="LONG" else (entry_px/final_px - 1.0)
    prior_mfes[sym].append(best_move)
    bounded = max(final_move, -SL)
    effective = bounded * lev
    new_eq = max(0.0, equity * (1.0 + effective))
    win = (effective>0)
    exit_date = series["dates"][i]
    exit_i = i
    return exit_i, exit_date, new_eq, win

# ── Backtest with EV tie-break & same-bar reopen ─────────────────────────────
def backtest_shared(start_date):
    series_map = {sym: prep_token(symbol, sym) for symbol, sym in SYMBOLS}

    # pointers per coin
    ptr = {}
    for sym, s in series_map.items():
        i=0
        while i<len(s["dates"]) and s["dates"][i]<start_date: i+=1
        ptr[sym]=max(i, LOOKBACK+1)

    # global date list (unique, sorted) from all coins
    all_dates = sorted(set(d for s in series_map.values() for d in s["dates"] if d>=start_date))

    equity=100.0; trades=0; wins=0
    max_dd=0.0; peak=equity
    prior_mfes={sym:[] for _,sym in SYMBOLS}

    d_idx=0
    while d_idx < len(all_dates):
        cur_date = all_dates[d_idx]

        # gather all *ready* candidates ON this date
        candidates=[]
        for sym, s in series_map.items():
            i = ptr[sym]
            if i is None or i>=len(s["dates"]): continue
            # advance pointer up to current date (but not beyond)
            while i < len(s["dates"]) and s["dates"][i] < cur_date:
                i += 1
            ptr[sym]=i
            if i < len(s["dates"]) and s["dates"][i]==cur_date:
                h = s["heats"][i]
                direction = dir_from_heat(h)
                if direction is not None:
                    conf = confidence_from_heat(h, direction)  # 77..100
                    tp = median(prior_mfes[sym]) if len(prior_mfes[sym])>=5 else TP_FALLBACKS.get(sym,0.03)
                    ev = conf * tp  # EV score for tie-break
                    candidates.append((ev, sym, i))

        if not candidates:
            d_idx += 1
            continue

        # choose best EV
        candidates.sort(reverse=True)   # highest EV first
        _, chosen_sym, entry_i = candidates[0]
        s = series_map[chosen_sym]

        # simulate that one trade exclusively
        exit_i, exit_date, equity, win = simulate_trade(s, entry_i, prior_mfes, equity)
        trades += 1
        if win: wins += 1

        # move chosen pointer to AFTER exit bar
        ptr[chosen_sym] = exit_i + 1

        # allow same-bar re-entry by others:
        # advance other pointers only for dates strictly < exit_date
        for sym2, ss in series_map.items():
            if sym2==chosen_sym: continue
            i=ptr[sym2]
            while i < len(ss["dates"]) and ss["dates"][i] < exit_date:
                i += 1
            ptr[sym2]=i

        # set loop date index to the exit date position (so we can open same-day)
        # find index of exit_date (it exists in all_dates)
        while d_idx < len(all_dates) and all_dates[d_idx] < exit_date:
            d_idx += 1
        # do NOT increment here; next loop evaluates candidates at exit_date

        # drawdown
        if equity>peak: peak=equity
        if peak>0:
            dd=(peak-equity)/peak
            if dd>max_dd: max_dd=dd

    winrate = (wins/trades*100.0) if trades>0 else 0.0
    roi_pct = (equity/100.0 - 1.0)*100.0
    return {"trades":trades,"wins":wins,"winrate":winrate,
            "equity":equity,"roi_pct":roi_pct,"max_dd_pct":max_dd*100.0}

# ── Run & print two periods ──────────────────────────────────────────────────
def run_periods():
    p1 = backtest_shared(datetime(2023,1,1).date())
    p2 = backtest_shared(datetime(2025,1,1).date())

    def show(title, r):
        print(f"\n=== {title} ===")
        print(f"Trades: {r['trades']}")
        print(f"Win rate: {r['winrate']:.1f}%")
        print(f"Final equity: ${r['equity']:.2f}  (ROI {r['roi_pct']:.1f}%)")
        print(f"Max drawdown: {r['max_dd_pct']:.1f}%")

    show("Shared Bankroll (EV tie-break) — from 2023-01-01", p1)
    show("Shared Bankroll (EV tie-break) — from 2025-01-01", p2)

    import sys; sys.stdout.flush()

if __name__ == "__main__":
    run_periods()
  
