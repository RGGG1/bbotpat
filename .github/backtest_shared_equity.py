#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Shared-bankroll backtest with per-coin confidence triggers & leverage caps
- One shared bankroll ($100 start), one trade at a time, same-bar close & reopen allowed
- Universe (excludes BNB, DOGE, LINK, XLM): BTC, ETH, SOL, ADA, TON, XRP, TRX, SUI, LTC
- Signals: heat from 20d z of daily returns; trigger is per-coin (see CONF_THRESHOLDS)
- TP: adaptive per-coin (walk-forward median MFE; fallback per coin)
- SL: 3% underlying
- Hold: max 96h (4 daily bars)
- Tie-break on same close: highest walk-forward hit rate; fallback highest market cap
- Per-coin leverage:
    * BTC/ETH/SOL: base 10×, pyramiding +1×/ +5% confidence up to 14×
    * Others (ADA/TON/XRP/TRX/SUI/LTC): fixed 2×, no pyramiding
"""

import requests, time
from datetime import datetime, timedelta

# ── Universe (excluded: BNB, DOGE, LINK, XLM) ────────────────────────────────
SYMBOLS = [
    ("BTCUSDT","BTC"), ("ETHUSDT","ETH"), ("SOLUSDT","SOL"),
    ("ADAUSDT","ADA"), ("TONUSDT","TON"), ("XRPUSDT","XRP"),
    ("TRXUSDT","TRX"), ("SUIUSDT","SUI"), ("LTCUSDT","LTC"),
]

# Static market-cap fallback priority (1 = highest cap among this set)
MCAP_PRIORITY = {
    "BTC": 1, "ETH": 2, "XRP": 3, "SOL": 4, "TON": 5,
    "ADA": 6, "TRX": 7, "LTC": 8, "SUI": 9,
}

# ── Algo params ──────────────────────────────────────────────────────────────
LOOKBACK      = 20
SL            = 0.03          # 3% (underlying) hard stop
HOLD_BARS     = 4             # 96h
CONF_PER_LEV  = 5             # +1× per +5% confidence increase

# Per-coin confidence triggers (77 for core; 90 for volatile alts)
CONF_THRESHOLDS = {
    "BTC": 77,
    "ETH": 77,
    "SOL": 77,
    "ADA": 90,
    "TON": 90,
    "XRP": 90,
    "TRX": 90,
    "SUI": 90,
    "LTC": 90,
}

# Adaptive TP fallbacks per coin (used until ≥5 MFEs recorded)
TP_FALLBACKS  = {
    "BTC":0.0227, "ETH":0.0167, "SOL":0.0444,
    "ADA":0.0300, "TON":0.0300, "XRP":0.0300,
    "TRX":0.0250, "SUI":0.0400, "LTC":0.0350
}

# Per-coin leverage policy
LEV_BASE = {
    "BTC":10, "ETH":10, "SOL":10,
    "ADA":2,  "TON":2,  "XRP":2,  "TRX":2,  "SUI":2,  "LTC":2
}
LEV_MAX = {
    "BTC":14, "ETH":14, "SOL":14,
    "ADA":2,  "TON":2,  "XRP":2,  "TRX":2,  "SUI":2,  "LTC":2
}
ALLOW_PYRAMID = {
    "BTC":True, "ETH":True, "SOL":True,
    "ADA":False, "TON":False, "XRP":False, "TRX":False, "SUI":False, "LTC":False
}

# ── Binance endpoints ────────────────────────────────────────────────────────
BASES = [
    "https://api.binance.com", "https://api1.binance.com",
    "https://api2.binance.com", "https://api3.binance.com",
    "https://data-api.binance.vision",
]
HEADERS = {"User-Agent":"shared-equity-percoin-conf-lev/1.0 (+github actions)"}

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

def triggered_direction(heat, sym):
    """Return LONG/SHORT/None based on per-coin trigger."""
    if heat is None: return None
    trig = CONF_THRESHOLDS.get(sym, 77)
    if heat >= trig:
        return "SHORT"
    if heat <= 100 - trig:
        return "LONG"
    return None

# ── Build per-token series ───────────────────────────────────────────────────
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

# ── Simulate one trade from entry index to exit ──────────────────────────────
def simulate_trade(series, entry_i, prior_mfes, equity):
    closes, heats, sym = series["closes"], series["heats"], series["sym"]
    entry_px = closes[entry_i]
    conf_trig = CONF_THRESHOLDS.get(sym, 77)

    direction = triggered_direction(heats[entry_i], sym)
    conf0 = heats[entry_i]

    lev   = LEV_BASE[sym]
    lev_max = LEV_MAX[sym]
    allow_pyr = ALLOW_PYRAMID[sym]

    tp    = (lambda arr, fb: (sorted([x for x in arr if x is not None])[len(arr)//2]
            if len(arr)>=5 else fb))(prior_mfes[sym], TP_FALLBACKS.get(sym,0.03))

    last_allowed = min(entry_i + HOLD_BARS, len(closes)-1)

    best_move = 0.0
    i = entry_i + 1
    while i < len(closes):
        cur_px = closes[i]
        move = (cur_px/entry_px - 1.0) if direction=="LONG" else (entry_px/cur_px - 1.0)
        if move>best_move: best_move = move

        h = heats[i]
        if h is not None:
            same = (triggered_direction(h, sym)==direction)
            inband = ((direction=="LONG" and h<=100-conf_trig) or
                      (direction=="SHORT" and h>=conf_trig))
            # Pyramiding (only for BTC/ETH/SOL)
            if allow_pyr and same and inband:
                stronger = (direction=="SHORT" and h>conf0) or (direction=="LONG" and h<conf0)
                if stronger:
                    steps=int(abs(h-conf0)//CONF_PER_LEV)
                    if steps>0:
                        lev=min(lev+steps, lev_max); conf0=h
                # Exit advisory if weaker + lower new TP and already >= it
                new_tp = (lambda arr, fb: (sorted([x for x in arr if x is not None])[len(arr)//2]
                        if len(arr)>=5 else fb))(prior_mfes[sym], TP_FALLBACKS.get(sym,0.03))
                weaker=(direction=="SHORT" and h<conf0) or (direction=="LONG" and h>conf0)
                if weaker and new_tp<tp and move>=new_tp:
                    break
            else:
                # Even without pyramiding, still allow exit-advisory logic
                new_tp = (lambda arr, fb: (sorted([x for x in arr if x is not None])[len(arr)//2]
                        if len(arr)>=5 else fb))(prior_mfes[sym], TP_FALLBACKS.get(sym,0.03))
                weaker=(direction=="SHORT" and h<conf0) or (direction=="LONG" and h>conf0)
                if same and inband and weaker and new_tp<tp and move>=new_tp:
                    break

        if move>=tp: break
        if move<=-SL: break
        if i>=last_allowed: break
        i+=1

    final_px = closes[i]
    final_move = (final_px/entry_px - 1.0) if direction=="LONG" else (entry_px/final_px - 1.0)

    prior_mfes[sym].append(best_move)

    # bounded loss, per-coin leverage, floor equity at 0
    bounded = max(final_move, -SL)
    effective = bounded * lev
    new_eq = max(0.0, equity * (1.0 + effective))
    win = (effective>0)
    exit_date = series["dates"][i]
    exit_i = i
    return exit_i, exit_date, new_eq, win

# ── Backtest with hit-rate tie-break & MCAP fallback ─────────────────────────
def backtest_shared(start_date):
    series_map = {sym: prep_token(symbol, sym) for symbol, sym in SYMBOLS}

    # per-coin pointers at/after start_date
    ptr = {}
    for sym, s in series_map.items():
        i=0
        while i<len(s["dates"]) and s["dates"][i] < start_date: i+=1
        ptr[sym]=max(i, LOOKBACK+1)

    # global date set
    all_dates = sorted(set(d for s in series_map.values() for d in s["dates"] if d>=start_date))

    equity=100.0; trades=0; wins=0
    max_dd=0.0; peak=equity
    prior_mfes={sym:[] for _,sym in SYMBOLS}

    # walk-forward hit-rate tallies
    hist_trades={sym:0 for _,sym in SYMBOLS}
    hist_wins  ={sym:0 for _,sym in SYMBOLS}

    d_idx=0
    while d_idx < len(all_dates):
        cur_date = all_dates[d_idx]

        # gather candidates on this date
        cands=[]
        for sym, s in series_map.items():
            i = ptr[sym]
            if i is None or i>=len(s["dates"]): continue
            # advance pointer to current date (but not past)
            while i < len(s["dates"]) and s["dates"][i] < cur_date:
                i += 1
            ptr[sym]=i
            if i < len(s["dates"]) and s["dates"][i]==cur_date:
                h = s["heats"][i]
                if triggered_direction(h, sym) is not None:
                    t = hist_trades[sym]; w = hist_wins[sym]
                    hit = (w / t) if t >= 1 else None   # use ≥1 trade for responsiveness; raise to ≥5 for stricter
                    cands.append((sym, i, hit))

        if not cands:
            d_idx += 1
            continue

        # choose best by hit-rate first; tie/None → MCAP fallback
        def sort_key(item):
            sym, idx, hit = item
            has = 1 if (hit is not None) else 0
            hit_val = hit if hit is not None else -1.0
            # Lower MCAP rank number → higher priority; invert for sorting
            mcap_rank = -MCAP_PRIORITY.get(sym, 99)
            return (has, hit_val, mcap_rank)

        cands.sort(key=sort_key, reverse=True)
        chosen_sym, entry_i, _ = cands[0]
        s = series_map[chosen_sym]

        # simulate that trade
        exit_i, exit_date, equity, win = simulate_trade(s, entry_i, prior_mfes, equity)
        trades += 1
        if win: 
            wins += 1
            hist_wins[chosen_sym] += 1
        hist_trades[chosen_sym] += 1

        # advance chosen pointer to after exit bar
        ptr[chosen_sym] = exit_i + 1

        # other pointers: advance only dates strictly < exit_date (allow same-bar re-entry)
        for sym2, ss in series_map.items():
            if sym2==chosen_sym: continue
            j = ptr[sym2]
            while j < len(ss["dates"]) and ss["dates"][j] < exit_date:
                j += 1
            ptr[sym2] = j

        # move global loop index up to exit_date (not past), so we can open same-bar
        while d_idx < len(all_dates) and all_dates[d_idx] < exit_date:
            d_idx += 1
        # no increment here; next iteration will process exit_date too

        # drawdown tracking
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

    show("Shared Bankroll (Per-coin triggers & leverage) — from 2023-01-01", p1)
    show("Shared Bankroll (Per-coin triggers & leverage) — from 2025-01-01", p2)

    import sys; sys.stdout.flush()

if __name__ == "__main__":
    run_periods()
       
