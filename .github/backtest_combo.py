#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Combo Backtest (close-only, deterministic)

- Baseline band strategy (ETH, SOL only):
    ENTER when heat >= 57 (SHORT) or <= 43 (LONG)
    EXIT  when heat returns inside softer band: 46 < heat < 54
  (Implements the 54–57% instruction as enter at 57, exit when <54 / >46)

- Leveraged strategy (BTC, ETH, SOL):
    ENTER when heat >= 77 (SHORT) or <= 23 (LONG)
    EXIT at first of: hit TP%, hit SL%, or 4 bars max hold
    TP% (fallback): BTC 2.27%, ETH 1.67%, SOL 4.44%
    SL%: 3%, leverage: 10× (no pyramiding in backtest for determinism)

- Daily, fully closed candles only (endTime = today UTC midnight - 1 ms)
- Single pooled balance starting at $100
- Priority rules:
    * Leveraged > Baseline (preempt baseline if needed)
    * If flat & both ETH and SOL baseline entries: pick SOL
    * If multiple leveraged entries: BTC > ETH > SOL

Reports compounded equity since 2023-01-01 and since 2025-01-01.
"""

import requests, math
from datetime import datetime, timezone, timedelta

# Universe and constants
COINS = [("BTCUSDT","BTC"), ("ETHUSDT","ETH"), ("SOLUSDT","SOL")]
BASES = [
    "https://api.binance.com",
    "https://api1.binance.com",
    "https://api2.binance.com",
    "https://api3.binance.com",
    "https://data-api.binance.vision",
]
HEADERS = {"User-Agent": "bbot-backtest/2.0"}

LOOKBACK = 20

# Heat thresholds
CONF_TRIGGER = 77           # leveraged enter (>=77 short, <=23 long)
BAND_ENTER   = 57           # baseline enter (>=57 short, <=43 long)
BAND_EXIT    = 54           # baseline exit back into 46..54

TP_FALLBACK = {"BTC": 0.0227, "ETH": 0.0167, "SOL": 0.0444}
SL = 0.03
HOLD_BARS = 4               # 4 daily bars

START_A = datetime(2023,1,1, tzinfo=timezone.utc)
START_B = datetime(2025,1,1, tzinfo=timezone.utc)

def binance_daily_closed(symbol, limit=1500):
    last_err=None
    for base in BASES:
        try:
            url=f"{base}/api/v3/klines"
            # only fully closed candles
            utc_now = datetime.utcnow()
            utc_mid = datetime(utc_now.year, utc_now.month, utc_now.day, tzinfo=timezone.utc)
            end_time_ms = int(utc_mid.timestamp()*1000) - 1
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
            continue
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
    z_signed = z_i if r_i>0 else -z_i
    h = 50 + 20*z_signed
    return max(0, min(100, round(h)))

def build_panels(cutoff_dt):
    """Return {sym: {'dates':[], 'closes':[], 'heats':[]}} from cutoff_dt to end."""
    out={}
    for symbol, sym in COINS:
        rows = binance_daily_closed(symbol)
        rows = [row for row in rows if row[0] >= cutoff_dt - timedelta(days=LOOKBACK+2)]
        if len(rows) < LOOKBACK+2:
            out[sym] = None; continue
        dates, closes = zip(*rows)
        closes = list(closes)
        rets = pct_returns(closes)
        zs   = zscore_series(rets, LOOKBACK)
        heats = [None]
        for i in range(1, len(closes)):
            heats.append(heat_from_ret_and_z(rets[i-1], zs[i-1]))
        out[sym] = {
            "dates": list(dates),
            "closes": closes,
            "heats": heats
        }
    return out

def leveraged_signal_today(h):
    if h is None: return None
    if h >= CONF_TRIGGER: return "SHORT"
    if h <= (100-CONF_TRIGGER): return "LONG"
    return None

def baseline_signal_today(h):
    if h is None: return None
    if h >= BAND_ENTER: return "SHORT"
    if h <= (100-BAND_ENTER): return "LONG"
    return None

def baseline_exit_today(h):
    if h is None: return False
    # exit if back inside (46,54) i.e., 100-BAND_EXIT < h < BAND_EXIT
    return ((100-BAND_EXIT) < h < BAND_EXIT)

def sim_combo(cutoff_dt):
    panels = build_panels(cutoff_dt)
    # Ensure ETH & SOL panels exist for baseline-only
    if panels.get("ETH") is None or panels.get("SOL") is None:
        return {"equity":100.0, "trades":0, "wins":0, "details":[]}

    # We’ll align by common date range across BTC/ETH/SOL
    common_dates = None
    for sym in ["BTC","ETH","SOL"]:
        p = panels.get(sym)
        if p is None: continue
        dset = set(p["dates"])
        common_dates = dset if common_dates is None else (common_dates & dset)
    if not common_dates:
        return {"equity":100.0, "trades":0, "wins":0, "details":[]}

    # Sort common dates and index maps per symbol
    dates_sorted = sorted([d for d in common_dates if d >= cutoff_dt])
    idx_map = {}
    for sym in ["BTC","ETH","SOL"]:
        p = panels.get(sym)
        # build map date->index
        m={}
        for i,d in enumerate(p["dates"]):
            m[d]=i
        idx_map[sym]=m

    equity = 100.0
    open_pos = None  # dict: {'type':'LEV'|'BAND','sym':...,'dir':...,'entry_i':...,'entry_px':...,'tp':float, 'deadline_i':int}
    trades=0; wins=0
    details=[]

    # Helper for leveraged exit detection (close-level, coarse)
    def check_lev_exit(sym, entry_i, dirn, tp, sl, deadline_i):
        p = panels[sym]
        entry_px = p["closes"][entry_i]
        last_i = min(deadline_i, len(p["closes"])-1)
        # walk from entry_i+1 .. last_i
        for i in range(entry_i+1, last_i+1):
            px = p["closes"][i]
            if dirn=="LONG":
                move = px/entry_px - 1.0
                if move >= tp:   return i, tp     # hit TP
                if move <= -sl:  return i, -sl    # hit SL
            else:
                move = entry_px/px - 1.0
                if move >= tp:   return i, tp
                if move <= -sl:  return i, -sl
        # no TP/SL; exit at last_i
        if dirn=="LONG":
            move = p["closes"][last_i]/entry_px - 1.0
        else:
            move = entry_px/p["closes"][last_i] - 1.0
        return last_i, move

    for d in dates_sorted:
        iB = idx_map["BTC"].get(d)
        iE = idx_map["ETH"].get(d)
        iS = idx_map["SOL"].get(d)
        # Skip if any panel missing index (shouldn't happen due to common_dates)
        if iB is None or iE is None or iS is None: 
            continue

        # Gather today's signals
        hB = panels["BTC"]["heats"][iB]
        hE = panels["ETH"]["heats"][iE]
        hS = panels["SOL"]["heats"][iS]

        lev_B = leveraged_signal_today(hB)
        lev_E = leveraged_signal_today(hE)
        lev_S = leveraged_signal_today(hS)

        band_E = baseline_signal_today(hE)
        band_S = baseline_signal_today(hS)

        # If we have an open position, manage it first
        if open_pos:
            if open_pos["type"] == "LEV":
                # Leveraged position exits only by its own rules; baseline is ignored until flat.
                # But if its deadline reached or TP/SL hit happened on the entry day, it's handled at creation.
                # Nothing to do here; exit is determined when placed (coarse close-only sim).
                pass
            else:
                # Baseline open — if any leveraged candidate exists today, preempt baseline:
                if lev_B or lev_E or lev_S:
                    # Close baseline at today's close
                    sym = open_pos["sym"]
                    i = idx_map[sym][d]
                    entry_i = open_pos["entry_i"]
                    entry_px = panels[sym]["closes"][entry_i]
                    exit_px  = panels[sym]["closes"][i]
                    if open_pos["dir"]=="LONG":
                        pct = exit_px/entry_px - 1.0
                    else:
                        pct = entry_px/exit_px - 1.0
                    equity *= (1.0 + pct)
                    trades += 1
                    wins += (1 if pct>0 else 0)
                    details.append((d.strftime("%Y-%m-%d"), f"Close BASE {sym} preempt", open_pos["dir"], pct, equity))

                    open_pos = None  # will consider opening the leveraged below

        # If flat now, evaluate entries by priority: leveraged first, then baseline (SOL>ETH)
        if not open_pos:
            # Leveraged candidates order: BTC > ETH > SOL
            chosen = None
            if lev_B:
                chosen=("BTC", lev_B)
            elif lev_E:
                chosen=("ETH", lev_E)
            elif lev_S:
                chosen=("SOL", lev_S)

            if chosen:
                sym, dirn = chosen
                # Set leveraged position and compute its exit (coarse, pre-computed)
                i = idx_map[sym][d]
                tp = TP_FALLBACK[sym]
                sl = SL
                deadline_i = min(i + HOLD_BARS, len(panels[sym]["closes"]) - 1)
                exit_i, move = check_lev_exit(sym, i, dirn, tp, sl, deadline_i)
                # Apply leverage 10×
                lev_ret = 10.0 * move
                equity *= (1.0 + lev_ret)
                trades += 1
                wins += (1 if lev_ret>0 else 0)
                details.append((d.strftime("%Y-%m-%d"), f"LEV {sym}", dirn, lev_ret, equity))
                # Remain flat after exit (we've accounted for PnL immediately at exit_i)
                # Advance loop naturally (we simulate daily, but PnL already booked).
                open_pos = None
                continue  # go next day

            # No leveraged entry; consider baseline (SOL preferred over ETH)
            chosen_base = None
            if band_S:
                chosen_base=("SOL", band_S)
            elif band_E:
                chosen_base=("ETH", band_E)

            if chosen_base:
                sym, dirn = chosen_base
                i = idx_map[sym][d]
                open_pos = {"type":"BAND","sym":sym,"dir":dirn,"entry_i":i}

        # If baseline open, check its exit condition (re-enters neutral band)
        if open_pos and open_pos["type"]=="BAND":
            sym = open_pos["sym"]
            i = idx_map[sym][d]
            h_today = panels[sym]["heats"][i]
            if baseline_exit_today(h_today):
                entry_i = open_pos["entry_i"]
                entry_px = panels[sym]["closes"][entry_i]
                exit_px  = panels[sym]["closes"][i]
                if open_pos["dir"]=="LONG":
                    pct = exit_px/entry_px - 1.0
                else:
                    pct = entry_px/exit_px - 1.0
                equity *= (1.0 + pct)
                trades += 1
                wins += (1 if pct>0 else 0)
                details.append((d.strftime("%Y-%m-%d"), f"EXIT BASE {sym}", open_pos["dir"], pct, equity))
                open_pos = None

    # If any baseline pos still open at the end, close at last close:
    if open_pos and open_pos["type"]=="BAND":
        sym = open_pos["sym"]
        last_i = idx_map[sym][dates_sorted[-1]]
        entry_i = open_pos["entry_i"]
        entry_px = panels[sym]["closes"][entry_i]
        exit_px  = panels[sym]["closes"][last_i]
        if open_pos["dir"]=="LONG":
            pct = exit_px/entry_px - 1.0
        else:
            pct = entry_px/exit_px - 1.0
        equity *= (1.0 + pct)
        trades += 1
        wins += (1 if pct>0 else 0)
        details.append((dates_sorted[-1].strftime("%Y-%m-%d"), f"FINAL EXIT BASE {sym}", open_pos["dir"], pct, equity))
        open_pos=None

    return {"equity": equity, "trades": trades, "wins": wins, "details": details}

def pretty(title, cutoff_dt):
    res = sim_combo(cutoff_dt)
    wr = (res["wins"]/res["trades"]*100.0) if res["trades"]>0 else 0.0
    print(f"\n=== {title} (from {cutoff_dt.date()} to last closed) ===")
    print(f"Trades: {res['trades']}, Win%: {wr:.1f}%, Final equity: ${res['equity']:.2f}  (×{res['equity']/100.0:.2f})")
    print("\nSample log (last 10 events):")
    for row in res["details"][-10:]:
        d, kind, side, pct, eq = row
        print(f"{d} | {kind} | {side:<5} | {pct*100:+.2f}% | Eq ${eq:,.2f}")

def main():
    pretty("COMBO: Leveraged (BTC/ETH/SOL) + Baseline band (ETH/SOL, 57 enter / 54 exit)", START_A)
    pretty("COMBO: Leveraged (BTC/ETH/SOL) + Baseline band (ETH/SOL, 57 enter / 54 exit)", START_B)

if __name__ == "__main__":
    main()
      
