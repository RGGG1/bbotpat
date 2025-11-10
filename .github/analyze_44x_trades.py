#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import time
import requests
from statistics import mean, pstdev, median
from datetime import datetime, timezone, timedelta

# ───────── Config ─────────
COINS = [
    ("BTCUSDT","BTC",0.0227),  # fallback TP% until >=5 prior MFEs exist
    ("ETHUSDT","ETH",0.0167),
    ("SOLUSDT","SOL",0.0444),
]

LOOKBACK    = 20                 # z-score window (days)
HOLD_DAYS   = 4                  # 96h cap
SL          = 0.005               # 3% stop (underlying)
START_DATE  = datetime(2023,1,1,tzinfo=timezone.utc)

# Heat edges (0..100 scale)
HEAT_SHORT  = 90                 # >= 90 → SHORT edge
HEAT_LONG   = 10                 # <= 10 → LONG edge

# Leverage ladder: base 10×, +1× per +5 heat distance beyond the edge, capped 14×
LEV_BASE    = 10
LEV_MAX     = 14
LEV_STEP    = 5

BASES = [
    "https://data-api.binance.vision",  # mirror first (often avoids 451)
    "https://api.binance.com",
    "https://api1.binance.com",
    "https://api2.binance.com",
    "https://api3.binance.com",
]
HEADERS = {"User-Agent":"analyze-44x/1.0 (+bbot)"}

# ───────── HTTP helpers ─────────
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
    Daily closes (date -> close) up to yesterday 23:59:59.999 UTC (i.e., only fully CLOSED candles).
    """
    now = datetime.utcnow().replace(tzinfo=timezone.utc)
    y   = now - timedelta(days=1)
    end_ms = int(datetime(y.year,y.month,y.day,23,59,59,tzinfo=timezone.utc).timestamp()*1000) + 999
    ks = binance_klines(symbol,"1d",1500,end_time_ms=end_ms)
    rows=[]
    for k in ks:
        ct = int(k[6])//1000           # closeTime (s)
        rows.append((datetime.utcfromtimestamp(ct).replace(tzinfo=timezone.utc), float(k[4])))
    return [(dt,px) for (dt,px) in rows if dt>=START_DATE]

def hourlies_between(symbol, start_dt, end_dt):
    """
    Fetch 1h closes in the interval (start_dt, end_dt], using kline OPEN times for startTime
    and CLOSE times to timestamp bars. We add +1s to start so we begin strictly after entry.
    """
    out=[]
    start_ms = int((start_dt + timedelta(seconds=1)).timestamp()*1000)
    end_ms   = int(end_dt.timestamp()*1000)

    while True:
        part = binance_klines(symbol, "1h", 1500, start_time_ms=start_ms, end_time_ms=end_ms)
        if not part:
            break
        for k in part:
            # k[0]=openTime(ms), k[6]=closeTime(ms), k[4]=close
            ct = int(k[6])//1000
            dt = datetime.utcfromtimestamp(ct).replace(tzinfo=timezone.utc)
            if dt <= start_dt or dt > end_dt:
                continue
            out.append((dt, float(k[4])))
        last_close = int(part[-1][6])
        if last_close >= end_ms:
            break
        start_ms = last_close + 1

    # dedupe by dt, keep last close
    ded={}
    for dt,px in out: ded[dt]=px
    return sorted(ded.items())

# ───────── Math helpers ─────────
def pct_returns(closes):
    return [closes[i]/closes[i-1]-1.0 for i in range(1,len(closes))]

def heat_series_aligned_to_day(closes, look=20):
    """
    Compute a 0..100 'heat' aligned to the day's return r[i] using a 20-day z of returns.
    Signed such that positive return → heat > 50 (overbought), negative return → heat < 50.
    """
    r = pct_returns(closes)
    out = [None]*len(closes)
    for i in range(len(closes)):
        if i < look or i >= len(closes)-1:
            continue
        window = r[i-look+1 : i+1]      # last 'look' returns, ending at r[i]
        if len(window) != look: continue
        mu = mean(window)
        sd = pstdev(window) if len(window)>1 else 0.0
        if sd <= 0: continue
        last_ret = r[i]
        z = (last_ret - mu)/sd
        z_signed = z if last_ret>0 else -z
        out[i] = max(0, min(100, round(50 + 20*z_signed)))
    return out

def adaptive_leverage(heat_today, direction):
    """
    Base 10×. Add +1× per +5 heat beyond the edge (≥90 SHORT, ≤10 LONG), cap 14×.
    """
    base = LEV_BASE
    if direction == "SHORT":
        dist = max(0, int(heat_today) - HEAT_SHORT)
    else:
        dist = max(0, HEAT_LONG - int(heat_today))
    steps = dist // LEV_STEP
    return min(LEV_MAX, base + int(steps))

# ───────── Main ─────────
def run():
    # Load daily data + heat for all coins
    daily={}
    for symbol,sym,tp_fb in COINS:
        rows=fully_closed_daily(symbol)
        if len(rows) < (LOOKBACK+5):
            raise RuntimeError(f"{sym}: insufficient daily data after {START_DATE.date()}")
        dts, cls = zip(*rows)
        heats = heat_series_aligned_to_day(list(cls), look=LOOKBACK)
        daily[sym]={
            "symbol":symbol,
            "dates":list(dts),
            "closes":list(cls),
            "heats":heats,
            "idx":{d:i for i,d in enumerate(dts)},
            "tp_fb":tp_fb
        }
        time.sleep(0.08)

    # Common calendar where all coins have valid heat
    common=set(daily["BTC"]["dates"]) & set(daily["ETH"]["dates"]) & set(daily["SOL"]["dates"])
    cal=[]
    for d in sorted(common):
        ok=True
        for sym in ("BTC","ETH","SOL"):
            i=daily[sym]["idx"][d]
            if daily[sym]["heats"][i] is None:
                ok=False; break
        if ok: cal.append(d)
    if not cal:
        raise RuntimeError("No aligned dates with valid heat for all three coins.")

    # Edge trigger (cross into band today vs yesterday)
    def edge_dir(sym, day):
        coin=daily[sym]; i=coin["idx"][day]
        if i==0: return None
        t=coin["heats"][i]; y=coin["heats"][i-1]
        if t is None or y is None: return None
        if t>=HEAT_SHORT and y<HEAT_SHORT: return "SHORT"
        if t<=HEAT_LONG  and y>HEAT_LONG:  return "LONG"
        return None

    # Adaptive TP via rolling median of prior MFE (underlying %)
    prior_mfe={"BTC":[], "ETH":[], "SOL":[]}
    def adaptive_tp(sym, fb):
        vals=[v for v in prior_mfe[sym] if v is not None]
        return (median(vals)/100.0) if len(vals)>=5 else fb

    equity=100.00
    trades=[]; active=None

    for day in cal:
        # 1) Close existing trade (simulate life to TP/SL/expiry)
        if active is not None:
            sym=active["sym"]; symbol=daily[sym]["symbol"]
            entry_dt=active["entry_dt"]; entry_px=active["entry_px"]
            tp_pct=active["tp_pct"]; direction=active["direction"]
            heat_today=active["heat"]; lev=active["lev"]
            valid_until=entry_dt + timedelta(days=HOLD_DAYS)

            hours=hourlies_between(symbol, entry_dt, valid_until)
            mae=mfe=0.0
            exit_dt=valid_until; exit_px=None; reason="expiry"

            # Underlying TP/SL prices
            tp_px = entry_px*(1+tp_pct) if direction=="LONG" else entry_px*(1-tp_pct)
            sl_px = entry_px*(1-SL)     if direction=="LONG" else entry_px*(1+SL)

            if hours:
                for dt,px in hours:
                    move = (px/entry_px-1.0) if direction=="LONG" else (entry_px/px-1.0)
                    mfe=max(mfe,move); mae=min(mae,move)
                    hit_tp = (direction=="LONG" and px>=tp_px) or (direction=="SHORT" and px<=tp_px)
                    hit_sl = (direction=="LONG" and px<=sl_px) or (direction=="SHORT" and px>=sl_px)
                    if hit_sl:
                        exit_dt,exit_px,reason = dt, sl_px, "sl"
                        break
                    if hit_tp:
                        exit_dt,exit_px,reason = dt, tp_px, "tp"
                        break
                if exit_px is None:
                    last_dt,last_px = hours[-1]
                    exit_dt,exit_px,reason = last_dt,last_px,"expiry"
            else:
                # rare: no hours returned (API/window issue)
                exit_dt,exit_px,reason = entry_dt,entry_px,"expiry(no-1h)"

            # Underlying ROI, then apply leverage
            roi_under = (exit_px/entry_px-1.0) if direction=="LONG" else (entry_px/exit_px-1.0)
            roi_eff   = roi_under * lev
            equity   *= (1.0 + roi_eff)

            trades.append({
                "sym":sym,"direction":direction,
                "entry_dt":entry_dt,"exit_dt":exit_dt,
                "entry_px":entry_px,"exit_px":exit_px,
                "tp_pct":tp_pct*100.0,
                "mae_pct":mae*100.0,"mfe_pct":mfe*100.0,
                "roi_pct":roi_eff*100.0,
                "lev":lev,"after":equity,"reason":reason
            })
            prior_mfe[sym].append(mfe*100.0)
            active=None  # slot freed

        # 2) If flat, see if any new edge triggers *today*; pick BTC>ETH>SOL
        if active is None:
            candidates=[]
            for sym in ("BTC","ETH","SOL"):
                dirn = edge_dir(sym, day)
                if not dirn: continue
                i=daily[sym]["idx"][day]
                entry_px=daily[sym]["closes"][i]
                tp_pct=adaptive_tp(sym, daily[sym]["tp_fb"])
                heat_today=daily[sym]["heats"][i]
                lev=adaptive_leverage(heat_today, dirn)
                candidates.append((sym,dirn,entry_px,tp_pct,heat_today,lev))
            if candidates:
                # priority: BTC > ETH > SOL
                sym,dirn,entry_px,tp_pct,heat_today,lev = sorted(
                    candidates, key=lambda x:("BTC","ETH","SOL").index(x[0])
                )[0]
                active={
                    "sym":sym,"symbol":daily[sym]["symbol"],
                    "direction":dirn,"entry_dt":day,
                    "entry_px":entry_px,"tp_pct":tp_pct,
                    "heat":heat_today,"lev":lev
                }

    # ───────── Output ─────────
    print("\n=== Backtest (golden 44× model) — single bankroll, compounded ===")
    print("Start balance: $100.00")
    if not trades:
        print("No trades generated.\n"); return

    header = f"{'#':>3}  {'SYM':<3}  {'Dir':<5}  {'Entry UTC':<16}  {'Exit UTC':<16}  {'TP%':>6}  {'MAE%':>7}  {'MFE%':>7}  {'ROI%':>7}  {'Lev':>4}  {'After$':>9}  {'Exit'}"
    print(header)
    for i,tr in enumerate(trades,1):
        print(f"{i:>3}  {tr['sym']:<3}  {tr['direction']:<5}  "
              f"{tr['entry_dt'].strftime('%Y-%m-%d %H:%M'):16}  "
              f"{tr['exit_dt'].strftime('%Y-%m-%d %H:%M'):16}  "
              f"{tr['tp_pct']:6.2f}  {tr['mae_pct']:7.2f}  {tr['mfe_pct']:7.2f}  "
              f"{tr['roi_pct']:7.2f}  {tr['lev']:4d}  {tr['after']:9.2f}  {tr['reason']}")

    final_equity = trades[-1]["after"]
    maes=[t["mae_pct"] for t in trades]; mfes=[t["mfe_pct"] for t in trades]; rois=[t["roi_pct"] for t in trades]
    print(f"\nSummary: Trades: {len(trades)}  |  Final equity: {final_equity/100.0:.2f}×  (${final_equity:,.2f})")
    print(f"Avg MAE: {mean(maes):.2f}%  |  Med MAE: {median(maes):.2f}%")
    print(f"Avg MFE: {mean(mfes):.2f}%  |  Med MFE: {median(mfes):.2f}%")
    print(f"Avg ROI/trade (lev-applied): {mean(rois):.2f}%  |  Med ROI: {median(rois):.2f}%\n")

if __name__=="__main__":
    run()
