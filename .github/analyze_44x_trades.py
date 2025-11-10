#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Adaptive Reversal v1.1 — faithful backtest (single trade, edge-trigger, 10x)

- Signal: cross INTO |z20| >= 2.5 on daily returns (edge trigger).
- Direction: contrarian (ret>0 -> SHORT, ret<0 -> LONG).
- Entry: at that day's CLOSE (Binance closeTime).
- Exit: first hit of TP (per-coin fallback %) or SL (3% underlying) checked hourly; else expiry at 96h.
- Leverage: fixed 10x (underlying move is multiplied by 10 for PnL).
- Exclusivity: only ONE trade active at a time. After exit, next eligible entry can be on the
  same calendar day's close if that close occurs AFTER the exit time.

Outputs a console table with each trade and a compounded equity trace.
"""

import time
import requests
from statistics import mean, pstdev, median
from datetime import datetime, timezone, timedelta

# ============================ Config ============================
COINS = [
    # (binance symbol, short sym, TP fallback (underlying %))
    ("BTCUSDT","BTC",0.0227),
    ("ETHUSDT","ETH",0.0167),
    ("SOLUSDT","SOL",0.0444),
]

Z_THRESH = 2.5
LOOKBACK = 20
SL_UNDERLYING = 0.03          # 3% underlying
HOLD_HOURS = 96
LEVERAGE = 10.0

START_DATE = datetime(2023,1,1,tzinfo=timezone.utc)
END_DATE   = None  # None = up to latest fully-closed daily

BASES = [
    "https://data-api.binance.vision",
    "https://api.binance.com",
    "https://api1.binance.com",
    "https://api2.binance.com",
    "https://api3.binance.com",
]
HEADERS = {"User-Agent":"ar-v1.1-backtest/1.0 (+bbotpat)"}


# ============================ HTTP helpers ============================
def binance_klines(symbol, interval, limit=1500, end_time_ms=None, start_time_ms=None, tries=6):
    params={"symbol":symbol,"interval":interval,"limit":limit}
    if end_time_ms is not None: params["endTime"]=end_time_ms
    if start_time_ms is not None: params["startTime"]=start_time_ms
    last_err=None; backoff=0.25
    for _ in range(tries):
        for base in BASES:
            try:
                r=requests.get(f"{base}/api/v3/klines", params=params, headers=HEADERS, timeout=30)
                if r.status_code in (451,403):
                    last_err = requests.HTTPError(f"{r.status_code} {r.reason}")
                    continue
                r.raise_for_status()
                return r.json()
            except Exception as e:
                last_err=e; time.sleep(backoff)
        backoff=min(2.0, backoff*1.8)
    raise last_err if last_err else RuntimeError("All Binance bases failed")


def fully_closed_daily(symbol):
    """
    Return list of (close_dt_utc (aware), close_price) for *completed* daily candles.
    If END_DATE is None, we stop at yesterday 23:59:59.999.
    """
    if END_DATE is None:
        now = datetime.utcnow().replace(tzinfo=timezone.utc)
        y   = now - timedelta(days=1)
        end_ms = int(datetime(y.year,y.month,y.day,23,59,59,tzinfo=timezone.utc).timestamp()*1000)+999
    else:
        end_ms = int(END_DATE.timestamp()*1000)
    ks = binance_klines(symbol,"1d",1500,end_time_ms=end_ms)
    rows=[]
    for k in ks:
        close_ms = int(k[6])
        dt = datetime.utcfromtimestamp(close_ms/1000.0).replace(tzinfo=timezone.utc)
        px = float(k[4])
        rows.append((dt,px))
    rows = [x for x in rows if x[0] >= START_DATE]
    return rows


def hourlies_between(symbol, start_dt, end_dt):
    """
    Return sorted list of (close_dt_utc, close_price) hourly candles within [start_dt, end_dt].
    De-duplicates by timestamp.
    """
    out=[]; start_ms=int(start_dt.timestamp()*1000)
    hard_end_ms=int((end_dt+timedelta(hours=1)).timestamp()*1000)
    while True:
        part=binance_klines(symbol,"1h",1500,start_time_ms=start_ms)
        if not part: break
        for k in part:
            close_ms=int(k[6])
            dt = datetime.utcfromtimestamp(close_ms/1000.0).replace(tzinfo=timezone.utc)
            if dt > end_dt: break
            out.append((dt,float(k[4])))
        start_ms=int(part[-1][6])+1
        if len(part)<1500 or start_ms>hard_end_ms or (out and out[-1][0]>=end_dt):
            break
    ded={}
    for dt,px in out: ded[dt]=px
    return sorted(ded.items())


# ============================ Math helpers ============================
def pct_returns(closes):
    return [closes[i]/closes[i-1]-1.0 for i in range(1,len(closes))]

def z_series(r, look=20):
    zs=[]
    for i in range(len(r)):
        if i+1 < look:
            zs.append(None); continue
        w=r[i+1-look:i+1]
        mu = sum(w)/len(w)
        # population std
        var = sum((x-mu)**2 for x in w)/len(w)
        sd = var**0.5
        zs.append(abs((r[i]-mu)/sd) if sd>0 else None)
    return zs


# ============================ Backtest core ============================
def run():
    # Load daily for each coin
    daily={}
    for symbol,sym,tp_fb in COINS:
        rows = fully_closed_daily(symbol)
        if len(rows) < LOOKBACK+2:
            raise RuntimeError(f"{sym}: insufficient daily data.")
        dts, closes = zip(*rows)
        r = pct_returns(list(closes))
        z = z_series(r, LOOKBACK)

        daily[sym] = {
            "symbol": symbol,
            "dates": list(dts),           # aware datetimes of the daily close
            "closes": list(closes),
            "rets": list(r),
            "z": list(z),
            "idx": {d:i for i,d in enumerate(dts)},
            "tp_fb": tp_fb
        }
        time.sleep(0.1)

    # Build a calendar where all coins have that day's z defined
    cal=set(daily["BTC"]["dates"]) & set(daily["ETH"]["dates"]) & set(daily["SOL"]["dates"])
    cal = sorted([d for d in cal if all(daily[s]["z"][daily[s]["idx"][d]] is not None for s in ("BTC","ETH","SOL"))])
    if not cal:
        raise RuntimeError("No aligned calendar.")

    priority = {"BTC":0,"ETH":1,"SOL":2}

    trades=[]
    equity=100.0
    next_allowed_idx = 0  # smallest index in cal we are allowed to enter on

    def edge_trigger(sym, d):
        i = daily[sym]["idx"][d]
        if i==0: return None
        t = daily[sym]["z"][i]; y = daily[sym]["z"][i-1]
        if t is None or y is None: return None
        if y < Z_THRESH <= t:
            # contrarian to the day's move
            dirn = "SHORT" if daily[sym]["rets"][i] > 0 else "LONG"
            return dirn
        return None

    k = 0
    while k < len(cal):
        day = cal[k]
        # skip if we're still within an active trade's blocking window
        if k < next_allowed_idx:
            k += 1
            continue

        # collect today's edge entries
        candidates=[]
        for sym in ("BTC","ETH","SOL"):
            ed = edge_trigger(sym, day)
            if ed:
                candidates.append((priority[sym], sym, ed))
        candidates.sort()

        if not candidates:
            k += 1
            continue

        # choose highest priority among today's signals
        _, sym, direction = candidates[0]
        coin = daily[sym]
        i = coin["idx"][day]
        entry_dt = coin["dates"][i]
        entry_px = coin["closes"][i]
        tp_pct = coin["tp_fb"]

        # simulate trade life using hourly bars
        valid_until = entry_dt + timedelta(hours=HOLD_HOURS)
        hours = hourlies_between(coin["symbol"], entry_dt, valid_until)

        # Prepare TP / SL prices on underlying
        if direction=="LONG":
            tp_px = entry_px*(1+tp_pct)
            sl_px = entry_px*(1-SL_UNDERLYING)
        else:
            tp_px = entry_px*(1-tp_pct)
            sl_px = entry_px*(1+SL_UNDERLYING)

        hit=None; exit_dt=valid_until; exit_px=None
        mae=0.0; mfe=0.0  # on *underlying*
        if hours:
            exit_px = hours[-1][1]  # default expiry exit = last hourly close
            for dt,px in hours:
                move = (px/entry_px-1.0) if direction=="LONG" else (entry_px/px-1.0)
                mfe = max(mfe, move)
                mae = min(mae, move)
                if direction=="LONG":
                    if px >= tp_px:
                        hit="tp"; exit_dt=dt; exit_px=px; break
                    if px <= sl_px:
                        hit="sl"; exit_dt=dt; exit_px=px; break
                else:
                    if px <= tp_px:
                        hit="tp"; exit_dt=dt; exit_px=px; break
                    if px >= sl_px:
                        hit="sl"; exit_dt=dt; exit_px=px; break
        else:
            # no hourlies -> assume flat expiry at entry
            exit_px = entry_px
            hit = None

        # compute ROI on underlying first, then apply leverage
        if direction=="LONG":
            under_roi = (exit_px/entry_px - 1.0)
        else:
            under_roi = (entry_px/exit_px - 1.0)
        lev_roi = under_roi * LEVERAGE

        # clamp lev_roi at SL / TP theoretical bounds to avoid accidental overshoot due to hourly closes
        if hit=="tp":
            lev_roi = tp_pct * LEVERAGE
        elif hit=="sl":
            lev_roi = -SL_UNDERLYING * LEVERAGE

        equity_after = equity * (1.0 + lev_roi)

        trades.append({
            "sym": sym,
            "direction": direction,
            "entry_dt": entry_dt,
            "exit_dt": exit_dt,
            "tp_pct": tp_pct*100.0,
            "mae_pct": mae*100.0,
            "mfe_pct": mfe*100.0,
            "under_roi_pct": under_roi*100.0,
            "lev_roi_pct": lev_roi*100.0,
            "entry_px": entry_px,
            "exit_px": exit_px,
            "after": equity_after,
            "exit_reason": hit or "expiry",
        })
        equity = equity_after

        # Advance calendar index to the first daily close >= exit_dt — allows same-day reentry
        # if exit happened earlier and a later daily close fires.
        # Find the smallest cal index whose datetime >= exit_dt
        j = k+1
        while j < len(cal) and cal[j] < exit_dt:
            j += 1
        next_allowed_idx = j
        k = j  # continue from there

    # ------------- print results -------------
    print("\n=== Backtest (Adaptive Reversal v1.1) — single bankroll, 10×, compounded ===")
    print(f"Start balance: $100.00")
    header = f"{'#':>3}  {'SYM':<3}  {'Dir':<5}  {'Entry UTC':<16}  {'Exit UTC':<16}  {'TP%':>6}  {'MAE%':>7}  {'MFE%':>7}  {'L-ROI%':>7}  {'After$':>9}  {'Exit'}"
    print(header)
    for idx,t in enumerate(trades,1):
        print(f"{idx:>3}  {t['sym']:<3}  {t['direction']:<5}  "
              f"{t['entry_dt'].strftime('%Y-%m-%d %H:%M'):16}  "
              f"{t['exit_dt'].strftime('%Y-%m-%d %H:%M'):16}  "
              f"{t['tp_pct']:6.2f}  {t['mae_pct']:7.2f}  {t['mfe_pct']:7.2f}  "
              f"{t['lev_roi_pct']:7.2f}  {t['after']:9.2f}  {t['exit_reason']}")
    if trades:
        print(f"\nSummary: Trades: {len(trades)}  |  Final equity: {trades[-1]['after']/100.0:.2f}×  (${trades[-1]['after']:.2f})")

if __name__=="__main__":
    run()
