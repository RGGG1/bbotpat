#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import time, requests
from statistics import mean, pstdev, median
from datetime import datetime, timezone, timedelta

# ---------------- Config ----------------
COINS = [
    ("BTCUSDT","BTC",0.0227),  # fallback TP% until >=5 prior MFEs exist
    ("ETHUSDT","ETH",0.0167),
    ("SOLUSDT","SOL",0.0444),
]

CONF_TRIGGER = 90          # <-- 44x model trigger
LOOKBACK     = 20          # days of returns for z
HOLD_BARS    = 4           # 96h
START_DATE   = datetime(2023,1,1,tzinfo=timezone.utc)

BASES = [
    "https://data-api.binance.vision",  # mirror first (avoids some 451s)
    "https://api.binance.com",
    "https://api1.binance.com",
    "https://api2.binance.com",
    "https://api3.binance.com",
]
HEADERS = {"User-Agent":"real-44x-analyzer/1.0 (+bbot)"}

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
                # Some regions get 451/403 intermittently
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
    Completed daily candles up to yesterday 23:59:59.999 UTC.
    We avoid the forming candle to match the live bot’s anchor.
    """
    now = datetime.utcnow().replace(tzinfo=timezone.utc)
    y   = now - timedelta(days=1)
    end_ms = int(datetime(y.year,y.month,y.day,23,59,59,tzinfo=timezone.utc).timestamp()*1000) + 999
    ks = binance_klines(symbol,"1d",1500,end_time_ms=end_ms)
    rows=[]
    for k in ks:
        ct=int(k[6])//1000
        rows.append((datetime.utcfromtimestamp(ct).replace(tzinfo=timezone.utc), float(k[4])))
    return [(dt,px) for (dt,px) in rows if dt>=START_DATE]

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
    # dedupe by close time
    ded={}
    for dt,px in out: ded[dt]=px
    return sorted((dt,px) for dt,px in ded.items() if start_dt<=dt<=end_dt)

# ------------- Math helpers -------------
def pct_returns(closes):
    return [closes[i]/closes[i-1]-1.0 for i in range(1,len(closes))]

def heat_series_aligned_to_day(closes, look=20):
    """
    heat[i] is aligned to *day i's return* (closes[i]/closes[i-1]-1),
    so an entry on day i uses that day's move.
    Returns a list of length len(closes) with floats or None.
    """
    r = pct_returns(closes)            # r[j] is return from day j to j+1
    out = [None]*len(closes)
    for i in range(len(closes)):
        # Need r[i] defined (i <= len(closes)-2), and a full window r[i-look+1 : i+1]
        if i < look or i >= len(closes)-1:
            continue
        window = r[i-look+1 : i+1]     # length = look, ending at r[i]
        if len(window) != look:
            continue
        mu = mean(window)
        sd = pstdev(window) if len(window)>1 else 0.0
        if sd <= 0:
            continue
        last_ret = r[i]
        z = (last_ret - mu)/sd
        z_signed = z if last_ret>0 else -z
        out[i] = max(0, min(100, round(50 + 20*z_signed)))
    return out

# ------------- Main -------------
def run():
    # Load daily for the 3 coins
    daily={}
    for symbol,sym,tp_fb in COINS:
        rows=fully_closed_daily(symbol)
        if len(rows) < (LOOKBACK+5):
            raise RuntimeError(f"{sym}: insufficient daily data.")
        dts, cls = zip(*rows)
        heats = heat_series_aligned_to_day(list(cls), look=LOOKBACK)
        daily[sym]={
            "symbol":symbol,
            "dates":list(dts),
            "closes":list(cls),
            "heat":heats,
            "idx":{d:i for i,d in enumerate(dts)},
            "tp_fb":tp_fb
        }
        time.sleep(0.1)

    # Aligned calendar where ALL coins have valid heat today AND yesterday
    common=set(daily["BTC"]["dates"]) & set(daily["ETH"]["dates"]) & set(daily["SOL"]["dates"])
    cal=[]
    for d in sorted(common):
        ok=True
        for sym in ("BTC","ETH","SOL"):
            i = daily[sym]["idx"][d]
            if i == 0:
                ok=False; break
            t = daily[sym]["heat"][i]
            y = daily[sym]["heat"][i-1]
            if t is None or y is None:
                ok=False; break
        if ok:
            cal.append(d)
    if not cal:
        raise RuntimeError("No aligned dates with valid (t,y) heat across all coins.")

    # Rolling adaptive TP memory (prior MFE%)
    prior_mfe={"BTC":[], "ETH":[], "SOL":[]}
    def adaptive_tp(sym, fb):
        vals=prior_mfe[sym]
        return (median(vals)/100.0) if len(vals)>=5 else fb

    # Edge trigger: CROSSING into the band on this day vs yesterday.
    # Guard against None everywhere.
    def edge_dir(sym, day):
        coin=daily[sym]; i=coin["idx"][day]
        if i == 0: return None
        t = coin["heat"][i]
        y = coin["heat"][i-1]
        if t is None or y is None:
            return None
        # SHORT when crossing up through threshold; LONG when crossing down through (100 - threshold)
        if (y < CONF_TRIGGER) and (t >= CONF_TRIGGER):
            return "SHORT"
        if (y > (100-CONF_TRIGGER)) and (t <= (100-CONF_TRIGGER)):
            return "LONG"
        return None

    trades=[]; active=None

    for day in cal:
        # 1) Close active first (simulate life: TP or 96h)
        if active is not None:
            sym=active["sym"]; symbol=daily[sym]["symbol"]
            entry_dt=active["entry_dt"]; entry_px=active["entry_px"]
            tp_pct=active["tp_pct"]; direction=active["direction"]
            valid_until=entry_dt + timedelta(days=HOLD_BARS)

            hours=hourlies_between(symbol, entry_dt, valid_until)
            mae=mfe=0.0
            exit_dt=valid_until; exit_px=entry_px; reason="expiry"
            if hours:
                tp_px = entry_px*(1+tp_pct) if direction=="LONG" else entry_px*(1-tp_pct)
                for dt,px in hours:
                    move = (px/entry_px-1.0) if direction=="LONG" else (entry_px/px-1.0)
                    mfe=max(mfe,move); mae=min(mae,move)
                    hit_tp = (direction=="LONG" and px>=tp_px) or (direction=="SHORT" and px<=tp_px)
                    if hit_tp:
                        exit_dt,exit_px,reason = dt,px,"tp"
                        break
            roi = (exit_px/entry_px-1.0) if direction=="LONG" else (entry_px/exit_px-1.0)
            trades.append({
                "sym":sym,"direction":direction,
                "entry_dt":entry_dt,"exit_dt":exit_dt,
                "entry_px":entry_px,"exit_px":exit_px,
                "hold_h":int(round((exit_dt-entry_dt).total_seconds()/3600)),
                "mae_pct":mae*100.0,"mfe_pct":mfe*100.0,"roi_pct":roi*100.0,
                "reason":reason
            })
            prior_mfe[sym].append(mfe*100.0)
            active=None  # free bankroll

        # 2) If free, check edge triggers today, priority BTC > ETH > SOL
        if active is None:
            candidates=[]
            for sym in ("BTC","ETH","SOL"):
                dirn = edge_dir(sym, day)
                if dirn:
                    i=daily[sym]["idx"][day]
                    candidates.append((sym,dirn,i))
            if candidates:
                sym,dirn,i = sorted(candidates,key=lambda x:("BTC","ETH","SOL").index(x[0]))[0]
                entry_px=daily[sym]["closes"][i]
                tp_pct=adaptive_tp(sym, daily[sym]["tp_fb"])
                active={
                    "sym":sym,"symbol":daily[sym]["symbol"],
                    "direction":dirn,"entry_dt":day,
                    "entry_px":entry_px,"tp_pct":tp_pct
                }

    # Print table (screen only)
    print("\n=== 44× Strategy (90% edge trigger, compounding — single active trade) ===\n")
    if not trades:
        print("No trades found.\n")
        return

    print(f"{'#':>3}  {'SYM':<3}  {'Dir':<5}  {'Entry UTC':<16}  {'Exit UTC':<16}  "
          f"{'Hh':>4}  {'Entry':>12}  {'Exit':>12}  {'MAE%':>7}  {'MFE%':>7}  {'ROI%':>7}  {'Exit'}")
    eq = 100.0
    for i,tr in enumerate(trades,1):
        eq *= (1 + tr["roi_pct"]/100.0)
        print(f"{i:>3}  {tr['sym']:<3}  {tr['direction']:<5}  "
              f"{tr['entry_dt'].strftime('%Y-%m-%d %H:%M'):16}  "
              f"{tr['exit_dt'].strftime('%Y-%m-%d %H:%M'):16}  "
              f"{tr['hold_h']:4d}  "
              f"{tr['entry_px']:12.4f}  {tr['exit_px']:12.4f}  "
              f"{tr['mae_pct']:7.2f}  {tr['mfe_pct']:7.2f}  {tr['roi_pct']:7.2f}  {tr['reason']}")
    rois=[t["roi_pct"] for t in trades]
    print("\nSummary:")
    print(f"Trades: {len(trades)}")
    print(f"Avg ROI: {mean(rois):.2f}% | Med ROI: {median(rois):.2f}%")
    print(f"Final equity on $100 start: ${eq:,.2f}  (×{eq/100:.2f})\n")

if __name__=="__main__":
    run()
         
