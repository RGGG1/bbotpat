#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import time, requests
from statistics import mean, median, pstdev
from datetime import datetime, timezone, timedelta

# ---------------- Config ----------------
COINS = [
    ("BTCUSDT","BTC",0.0227),
    ("ETHUSDT","ETH",0.0167),
    ("SOLUSDT","SOL",0.0444),
]

START_DATE = datetime(2023,1,1,tzinfo=timezone.utc)
LOOKBACK   = 20              # for z
Z_THRESH   = 2.5             # absolute z trigger
SL         = 0.03            # 3% stop (underlying)
HOLD_DAYS  = 4               # 96h
START_EQUITY = 100.0

BASES = [
    "https://data-api.binance.vision",
    "https://api.binance.com",
    "https://api1.binance.com",
    "https://api2.binance.com",
    "https://api3.binance.com",
]
HEADERS = {"User-Agent":"ar_v11_backtest/1.0 (+bbot)"}

# ------------- HTTP helpers -------------
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
    # up to yesterday 23:59:59.999 UTC
    now = datetime.utcnow().replace(tzinfo=timezone.utc)
    y   = now - timedelta(days=1)
    end_ms = int(datetime(y.year,y.month,y.day,23,59,59,tzinfo=timezone.utc).timestamp()*1000)+999
    ks = binance_klines(symbol,"1d",1500,end_time_ms=end_ms)
    rows=[]
    for k in ks:
        ct = int(k[6])//1000  # close time
        dt = datetime.utcfromtimestamp(ct).replace(tzinfo=timezone.utc)
        px = float(k[4])
        rows.append((dt.date(), px))
    return [(dt,px) for (dt,px) in rows if datetime(dt.year,dt.month,dt.day,tzinfo=timezone.utc)>=START_DATE]

def hourlies_between(symbol,start_dt,end_dt):
    out=[]; start_ms=int(start_dt.timestamp()*1000)
    hard_end_ms=int((end_dt+timedelta(hours=1)).timestamp()*1000)
    while True:
        part=binance_klines(symbol,"1h",1500,start_time_ms=start_ms)
        if not part: break
        for k in part:
            ct=int(k[6])//1000
            dt=datetime.utcfromtimestamp(ct).replace(tzinfo=timezone.utc)
            if dt> end_dt: break
            out.append((dt,float(k[4])))
        start_ms=int(part[-1][6])+1
        if len(part)<1500 or start_ms>hard_end_ms or (out and out[-1][0]>=end_dt):
            break
    # dedupe
    seen={}
    for dt,px in out: seen[dt]=px
    return sorted(seen.items())

# ------------- Math helpers -------------
def pct_returns(closes):
    return [closes[i]/closes[i-1]-1.0 for i in range(1,len(closes))]

def z_aligned_to_dates(closes, look=20):
    """
    Return z array with SAME LENGTH as closes/dates:
    z[i] uses returns up to day i (window ending at return r[i-1]).
    z[0] = None; first valid index is i where i>=look.
    """
    r = pct_returns(closes)            # len N-1
    N = len(closes)
    z = [None]*N
    # to compute z for day i, we need window r[i-look : i] of length 'look'
    for i in range(look, N):
        window = r[i-look:i]           # length look, last element is r[i-1]
        mu = mean(window)
        sd = pstdev(window) if look>1 else 0.0
        if sd and sd>0:
            last_ret = r[i-1]
            z[i] = abs((last_ret - mu)/sd)
        else:
            z[i] = None
    return z

def direction_from_last_ret(closes, i):
    """Return 'LONG' if last daily move was down, else 'SHORT'."""
    if i==0: return None
    ret = closes[i]/closes[i-1]-1.0
    return "LONG" if ret<0 else "SHORT"

# ------------- Backtest -------------
def run():
    # Load dailies + aligned z per coin
    daily={}
    for symbol,sym,tp_fb in COINS:
        rows=fully_closed_daily(symbol)
        if len(rows)<LOOKBACK+2:
            raise RuntimeError(f"{sym}: insufficient daily data.")
        dts, cls = zip(*rows)  # dts are date objects
        cls=list(cls); dts=list(dts)
        z = z_aligned_to_dates(cls, look=LOOKBACK)
        daily[sym]={
            "symbol":symbol,
            "dates":dts,                # list of date (UTC)
            "closes":cls,               # list of closes
            "z":z,                      # same length as dates
            "idx":{d:i for i,d in enumerate(dts)},
            "tp_fb":tp_fb
        }
        time.sleep(0.1)

    # calendar: only the days that exist for ALL coins and have valid z for ALL
    common=set(daily["BTC"]["dates"]) & set(daily["ETH"]["dates"]) & set(daily["SOL"]["dates"])
    cal=[]
    for d in sorted(common):
        ok=True
        for s in ("BTC","ETH","SOL"):
            i = daily[s]["idx"][d]
            # guard index and z validity
            if i>=len(daily[s]["z"]) or daily[s]["z"][i] is None:
                ok=False; break
        if ok: cal.append(d)
    if not cal:
        raise RuntimeError("No aligned dates with valid z.")

    # Adaptive TP memory (per coin)
    prior_mfe={"BTC":[], "ETH":[], "SOL":[]}
    def adaptive_tp(sym):
        vals=prior_mfe[sym]
        if len(vals)>=5:
            return median(vals)/100.0
        return next(tp for _,s,tp in COINS if s==sym)

    equity = START_EQUITY
    trades=[]
    active=None   # None or dict

    def simulate_trade(sym, entry_date, entry_px, direction, tp_pct):
        """Simulate up to HOLD_DAYS with hourlies; return dict with exit info + MAE/MFE/ROI."""
        symbol = daily[sym]["symbol"]
        start_dt = datetime(entry_date.year, entry_date.month, entry_date.day, 23, 59, tzinfo=timezone.utc)
        end_dt   = start_dt + timedelta(days=HOLD_DAYS)
        hours = hourlies_between(symbol, start_dt, end_dt)

        tp_px = entry_px*(1+tp_pct) if direction=="LONG" else entry_px*(1-tp_pct)
        sl_px = entry_px*(1-SL)     if direction=="LONG" else entry_px*(1+SL)

        mae=0.0; mfe=0.0
        exit_dt=end_dt; exit_px=None; reason="expiry"

        if hours:
            for dt,px in hours:
                move = (px/entry_px-1.0) if direction=="LONG" else (entry_px/px-1.0)
                mfe = max(mfe, move)
                mae = min(mae, move)
                # hit checks
                hit_tp = (direction=="LONG" and px>=tp_px) or (direction=="SHORT" and px<=tp_px)
                hit_sl = (direction=="LONG" and px<=sl_px) or (direction=="SHORT" and px>=sl_px)
                if hit_tp:
                    exit_dt, exit_px, reason = dt, px, "tp"
                    break
                if hit_sl:
                    exit_dt, exit_px, reason = dt, px, "sl"
                    break
            if exit_px is None:
                # expiry → take last hourly close
                exit_dt, exit_px = hours[-1]
        else:
            # no hourlies (rare) → use the daily close at end day if present, else stay at entry
            end_day = (start_dt + timedelta(days=HOLD_DAYS)).date()
            coin = daily[sym]
            if end_day in coin["idx"]:
                j = coin["idx"][end_day]
                if j < len(coin["closes"]):
                    exit_px = coin["closes"][j]
                else:
                    exit_px = entry_px
            else:
                exit_px = entry_px

        roi = (exit_px/entry_px-1.0) if direction=="LONG" else (entry_px/exit_px-1.0)
        return {
            "entry_dt": start_dt, "exit_dt": exit_dt,
            "entry_px": entry_px, "exit_px": exit_px,
            "mae_pct": mae*100.0, "mfe_pct": mfe*100.0,
            "roi_pct": roi*100.0, "reason": reason
        }

    for day in cal:
        # close active if exists by simulating from its recorded entry
        if active is not None:
            sim = simulate_trade(active["sym"], active["entry_date"], active["entry_px"], active["direction"], active["tp_pct"])
            equity *= (1.0 + sim["roi_pct"]/100.0)
            trades.append({
                "#": len(trades)+1,
                "sym": active["sym"],
                "direction": active["direction"],
                "entry_dt": sim["entry_dt"], "exit_dt": sim["exit_dt"],
                "tp_pct": active["tp_pct"]*100.0,
                "mae_pct": sim["mae_pct"], "mfe_pct": sim["mfe_pct"], "roi_pct": sim["roi_pct"],
                "after": equity, "reason": sim["reason"]
            })
            # learn MFE for future adaptive TP
            prior_mfe[active["sym"]].append(sim["mfe_pct"])
            active=None

        # after closing (possibly on this same calendar day), we may open a new trade on this day
        # Determine signals for this calendar day
        candidates=[]
        for sym in ("BTC","ETH","SOL"):
            coin=daily[sym]; i=coin["idx"][day]
            z=coin["z"][i]
            if z is None: continue
            if z >= Z_THRESH:
                direction = direction_from_last_ret(coin["closes"], i)
                entry_px  = coin["closes"][i]
                tp_pct    = adaptive_tp(sym)
                candidates.append((sym, direction, entry_px, tp_pct, day))

        if candidates and active is None:
            # priority: BTC > ETH > SOL
            order = {"BTC":0,"ETH":1,"SOL":2}
            candidates.sort(key=lambda x: order[x[0]])
            sym, direction, entry_px, tp_pct, entry_day = candidates[0]
            active = {"sym":sym, "direction":direction, "entry_px":entry_px, "entry_date":entry_day, "tp_pct":tp_pct}

    # pretty print
    print("\n=== Backtest (AR v1.1) — single bankroll, compounded ===")
    print(f"Start balance: ${START_EQUITY:,.2f}")
    print(f"{'#':>3}  {'SYM':<3}  {'Dir':<5}  {'Entry UTC':<16}  {'Exit UTC':<16}  {'TP%':>6}  {'MAE%':>7}  {'MFE%':>7}  {'ROI%':>7}  {'After$':>9}  {'Exit'}")
    for t in trades:
        print(f"{t['#']:>3}  {t['sym']:<3}  {t['direction']:<5}  "
              f"{t['entry_dt'].strftime('%Y-%m-%d %H:%M'):16}  "
              f"{t['exit_dt'].strftime('%Y-%m-%d %H:%M'):16}  "
              f"{t['tp_pct']:6.2f}  {t['mae_pct']:7.2f}  {t['mfe_pct']:7.2f}  {t['roi_pct']:7.2f}  {t['after']:9.2f}  {t['reason']}")

    if trades:
        final = trades[-1]["after"]
        avg_roi = sum(t["roi_pct"] for t in trades)/len(trades)
        print(f"\nSummary: Trades: {len(trades)}  |  Final equity: {final/START_EQUITY:.2f}×  (${final:,.2f})")
        print(f"Avg ROI/trade: {avg_roi:.2f}%")
    else:
        print("\nSummary: No trades generated.")

if __name__=="__main__":
    run()
