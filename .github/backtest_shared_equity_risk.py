#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Shared-bankroll backtest + per-token risk analysis
- One shared bankroll ($100 start), one trade at a time, same-bar close & reopen allowed
- Universe (excludes BNB, DOGE, LINK, XLM): BTC, ETH, SOL, ADA, TON, XRP, TRX, SUI, LTC
- Signals: heat from 20d z of daily returns; >=77 SHORT, <=23 LONG
- TP: adaptive per-coin (walk-forward median MFE; fallback per coin)
- SL: 3% underlying
- Hold: max 96h (4 daily bars)
- Leverage: 10× base, pyramiding +1× per +5% confidence up to 14×
- Tie-break when multiple signals on same close:
    1) Highest walk-forward hit rate (wins/trades)
    2) If tie/insufficient history → highest market cap priority (static)
- Outputs for each period (2023→now and 2025→now):
    - Portfolio summary + per-token risk table and Top-5 variance contributors
"""

import requests, time, math, statistics
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
CONF_TRIGGER  = 77
SL            = 0.03          # 3% (underlying) hard stop
HOLD_BARS     = 4             # 96h
BASE_LEV      = 10
MAX_LEV       = 14
CONF_PER_LEV  = 5             # +1× per +5% confidence increase

# Adaptive TP fallbacks per coin (used until ≥5 MFEs recorded)
TP_FALLBACKS  = {
    "BTC":0.0227, "ETH":0.0167, "SOL":0.0444,
    "ADA":0.0300, "TON":0.0300, "XRP":0.0300,
    "TRX":0.0250, "SUI":0.0400, "LTC":0.0350
}

# ── Binance endpoints ────────────────────────────────────────────────────────
BASES = [
    "https://api.binance.com", "https://api1.binance.com",
    "https://api2.binance.com", "https://api3.binance.com",
    "https://data-api.binance.vision",
]
HEADERS = {"User-Agent":"shared-equity-hitrate-mcap-risk/1.0 (+github actions)"}

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

def median(a):
    v=[x for x in a if x is not None]; v.sort()
    n=len(v); 
    if n==0: return None
    return v[n//2] if n%2 else (v[n//2-1]+v[n//2])/2.0

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
            # Pyramiding
            if same and inband:
                stronger = (direction=="SHORT" and h>conf0) or (direction=="LONG" and h<conf0)
                if stronger:
                    steps=int(abs(h-conf0)//CONF_PER_LEV)
                    if steps>0:
                        lev=min(lev+steps, MAX_LEV); conf0=h
                # Exit advisory if weaker + lower new TP and already >= it
                new_tp = median(prior_mfes[sym]) if len(prior_mfes[sym])>=5 else TP_FALLBACKS.get(sym,0.03)
                weaker=(direction=="SHORT" and h<conf0) or (direction=="LONG" and h>conf0)
                if weaker and new_tp<tp and move>=new_tp:
                    break

        if move>=tp: break
        if move<=-SL: break
        if i>=last_allowed: break
        i+=1

    final_px = closes[i]
    final_move = (final_px/entry_px - 1.0) if direction=="LONG" else (entry_px/final_px - 1.0)

    prior_mfes[sym].append(best_move)

    # bounded loss, pyramiding-enabled leverage, floor equity at 0
    bounded = max(final_move, -SL)
    effective = bounded * lev              # leveraged % change for the trade
    new_eq = max(0.0, equity * (1.0 + effective))
    win = (effective>0)
    exit_date = series["dates"][i]
    exit_i = i

    # return detailed log info
    trade_log = {
        "sym": sym,
        "entry_i": entry_i,
        "exit_i": exit_i,
        "entry_px": entry_px,
        "exit_px": final_px,
        "direction": "LONG" if direction=="LONG" else "SHORT",
        "lev": lev,
        "underlying_move_pct": final_move*100.0,
        "effective_pct": effective*100.0,     # leveraged result (%)
        "exit_date": exit_date,
    }

    return exit_i, exit_date, new_eq, win, trade_log

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

    trades_log=[]

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
                if dir_from_heat(h) is not None:
                    t = hist_trades[sym]; w = hist_wins[sym]
                    hit = (w / t) if t >= 1 else None   # use ≥1 for responsiveness; tighten to ≥5 if you prefer
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
        chosen_sym, entry_i, chosen_hit = cands[0]
        s = series_map[chosen_sym]

        equity_before = equity
        exit_i, exit_date, equity, win, tlog = simulate_trade(s, entry_i, prior_mfes, equity)
        trades += 1
        if win:
            wins += 1
            hist_wins[chosen_sym] += 1
        hist_trades[chosen_sym] += 1

        tlog["equity_before"] = equity_before
        tlog["equity_after"]  = equity
        tlog["win"] = win
        tlog["hit_used"] = chosen_hit
        trades_log.append(tlog)

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
    return {
        "trades":trades,"wins":wins,"winrate":winrate,
        "equity":equity,"roi_pct":roi_pct,"max_dd_pct":max_dd*100.0,
        "trades_log": trades_log
    }

# ── Risk summary helpers ─────────────────────────────────────────────────────
def summarize_risk(trades_log):
    # per-token arrays of leveraged returns (%)
    per = {}
    for t in trades_log:
        sym = t["sym"]
        per.setdefault(sym, []).append(t["effective_pct"])

    # overall variance denominator
    all_sq = sum((x/100.0)**2 for arr in per.values() for x in arr) + 1e-12

    rows=[]
    for sym, arr in per.items():
        n = len(arr)
        mean = statistics.mean(arr) if n else 0.0
        stdev = statistics.pstdev(arr) if n>1 else 0.0
        worst = min(arr) if n else 0.0
        sharpe_like = mean / (stdev + 1e-9)
        var_share = (sum((x/100.0)**2 for x in arr) / all_sq) * 100.0
        rows.append({
            "sym": sym,
            "trades": n,
            "mean_pct": mean,
            "stdev_pct": stdev,
            "worst_pct": worst,
            "sharpe_like": sharpe_like,
            "var_share_pct": var_share
        })
    # sort by variance share desc
    rows.sort(key=lambda r: r["var_share_pct"], reverse=True)
    return rows

def print_risk_table(title, rows):
    print(f"\n--- {title}: Per-Token Risk (leveraged trade % statistics) ---")
    print(f"{'SYM':<6} {'Trades':>6} {'Mean%':>9} {'Std%':>9} {'Worst%':>9} {'Sharpe':>8} {'Var%':>8}")
    for r in rows:
        print(f"{r['sym']:<6} {r['trades']:>6} {r['mean_pct']:>8.2f} {r['stdev_pct']:>8.2f} {r['worst_pct']:>8.2f} {r['sharpe_like']:>7.2f} {r['var_share_pct']:>7.2f}")

    # Top-5 variance contributors
    top = rows[:5]
    names = ", ".join([f"{x['sym']} ({x['var_share_pct']:.1f}%)" for x in top])
    print(f"Top variance contributors: {names}")

# ── Run & print two periods ──────────────────────────────────────────────────
def run_periods():
    startA = datetime(2023,1,1).date()
    startB = datetime(2025,1,1).date()

    for start in (startA, startB):
        res = backtest_shared(start)
        print(f"\n=== Shared Bankroll (Hit-rate; MCAP fallback) — from {start} ===")
        print(f"Trades: {res['trades']}")
        print(f"Win rate: {res['winrate']:.1f}%")
        print(f"Final equity: ${res['equity']:.2f}  (ROI {res['roi_pct']:.1f}%)")
        print(f"Max drawdown: {res['max_dd_pct']:.1f}%")

        risk_rows = summarize_risk(res["trades_log"])
        print_risk_table(f"{start} → last closed", risk_rows)

    import sys; sys.stdout.flush()

if __name__ == "__main__":
    run_periods()
  
