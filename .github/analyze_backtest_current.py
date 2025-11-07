#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import time, requests
from statistics import median, mean, pstdev
from datetime import datetime, timezone, timedelta

# ---------- Config (matches your algo) ----------
COINS = [
    ("BTCUSDT","BTC",0.0227),
    ("ETHUSDT","ETH",0.0167),
    ("SOLUSDT","SOL",0.0444),
]

Z_THRESH   = 2.5        # entry trigger (abs z of daily return)
SL         = 0.03       # 3% stop (underlying)
HOLD_DAYS  = 4          # 96h cap
LOOKBACK   = 20         # z-score window
START_DATE = datetime(2023,1,1,tzinfo=timezone.utc)

# Binance + mirror rotation
BASES   = ["https://api.binance.com","https://api1.binance.com","https://api2.binance.com","https://api3.binance.com","https://data-api.binance.vision"]
HEADERS = {"User-Agent": "bbot-backtest/1.0"}

# ---------- HTTP helpers ----------
def binance_klines(symbol, interval, limit=1500, end_time_ms=None, start_time_ms=None):
    params={"symbol":symbol,"interval":interval,"limit":limit}
    if end_time_ms is not None: params["endTime"]=end_time_ms
    if start_time_ms is not None: params["startTime"]=start_time_ms
    last_err=None
    for base in BASES:
        try:
            r=requests.get(f"{base}/api/v3/klines",params=params,headers=HEADERS,timeout=30)
            if r.status_code in (403,451):  # geo/rate blocked — try next
                last_err=Exception(f"{r.status_code} {r.reason}"); continue
            r.raise_for_status()
            return r.json()
        except Exception as e:
            last_err=e; continue
    raise last_err if last_err else RuntimeError("All Binance bases failed")

def fully_closed_daily(symbol):
    """Daily kline closes up to *yesterday* 23:59:59.999 UTC."""
    now = datetime.utcnow().replace(tzinfo=timezone.utc)
    y   = now - timedelta(days=1)
    end_ms = int(datetime(y.year,y.month,y.day,23,59,59,tzinfo=timezone.utc).timestamp()*1000) + 999
    ks = binance_klines(symbol,"1d",1500,end_time_ms=end_ms)
    out=[]
    for k in ks:
        close_ts = int(k[6])//1000
        dt = datetime.utcfromtimestamp(close_ts).replace(tzinfo=timezone.utc)
        px = float(k[4])
        if dt>=START_DATE: out.append((dt,px))
    return out

def hourlies_between(symbol,start_dt,end_dt):
    """Hourly closes [start_dt, end_dt] inclusive."""
    start_ms = int(start_dt.timestamp()*1000)
    hard_end_ms = int(end_dt.timestamp()*1000)
    out=[]
    while True:
        part=binance_klines(symbol,"1h",1500,start_time_ms=start_ms)
        if not part: break
        for k in part:
            ct=int(k[6])//1000
            dt=datetime.utcfromtimestamp(ct).replace(tzinfo=timezone.utc)
            if dt> end_dt: break
            out.append((dt,float(k[4])))
        start_ms=int(part[-1][6])+1
        if len(part)<1500 or (out and out[-1][0]>=end_dt): break
    # de-dup by dt
    ded={dt:px for dt,px in out}
    seq = sorted((dt,px) for dt,px in ded.items() if start_dt<=dt<=end_dt)
    return seq

# ---------- Stats ----------
def pct_returns(closes):
    return [closes[i]/closes[i-1]-1.0 for i in range(1,len(closes))]

def zscores_of_returns(closes, look=20):
    r=pct_returns(closes)
    zs=[None]*len(r)
    for i in range(len(r)):
        j0=i+1-look
        if j0<0: continue
        w=r[j0:i+1]
        mu=sum(w)/look
        sd=pstdev(w) if look>1 else 0.0
        if sd>0: zs[i]=abs((r[i]-mu)/sd)
    return zs, r

# ---------- Backtest ----------
def run():
    # Load dailies
    daily={}
    for symbol,sym,tp_fb in COINS:
        rows=fully_closed_daily(symbol)
        if len(rows)<(LOOKBACK+5):
            raise RuntimeError(f"{sym}: not enough daily data.")
        dts, cls = zip(*rows)
        zs, rets = zscores_of_returns(list(cls), LOOKBACK)  # zs aligned to rets[i] (move from day i to i+1)
        daily[sym]={
            "symbol":symbol,
            "dates":list(dts),
            "closes":list(cls),
            "zs":zs,
            "rets":rets,
            "idx":{d:i for i,d in enumerate(dts)},
            "tp_fb":tp_fb
        }
        time.sleep(0.1)

    # Aligned calendar
    cal = sorted(set(daily["BTC"]["dates"]) & set(daily["ETH"]["dates"]) & set(daily["SOL"]["dates"]))
    # We can only form a z on rets[i] → entry is at dates[i+1]; but simpler:
    # Treat "signal on day D" as: find index i for D, check zs[i] (the move ending at D).
    # That matches your live logic of using "latest closed candle".

    # Adaptive TP store (rolling median of prior MFEs per coin)
    prior_mfe = {"BTC":[], "ETH":[], "SOL":[]}
    def adaptive_tp(sym, fb):
        arr=prior_mfe[sym]
        return (median(arr)/100.0) if len(arr)>=5 else fb

    # Account
    balance = 100.0
    in_trade = None  # dict with exit scheduling pre-computed
    trades=[]

    # Helper to settle a trade (already has computed exit)
    def settle(tr):
        nonlocal balance
        roi = tr["roi"]
        balance *= (1.0 + roi)
        tr["after_bal"] = balance
        trades.append(tr)
        # feed MFE history (percent)
        prior_mfe[tr["sym"]].append(tr["mfe_pct"])

    # Precompute a map from date->signals (BTC/ETH/SOL pass Z_THRESH that day)
    def day_signal(sym, day):
        coin=daily[sym]
        i=coin["idx"].get(day)
        if i is None or i==0: return None
        z = coin["zs"][i-1]  # return that ended at 'day'
        if z is None: return None
        if z >= Z_THRESH:
            direction = "SHORT" if coin["rets"][i-1] > 0 else "LONG"
            entry_px  = coin["closes"][i]      # entry = close of 'day'
            entry_dt  = coin["dates"][i]
            return (direction, entry_px, entry_dt)
        return None

    # Simulate trade exit by hourlies (TP/SL/expiry) and compute MFE/MAE
    def simulate_exit(sym, direction, entry_dt, entry_px, tp_pct):
        symbol = daily[sym]["symbol"]
        valid_until = entry_dt + timedelta(days=HOLD_DAYS)
        hours = hourlies_between(symbol, entry_dt, valid_until)
        tp_px = entry_px*(1+tp_pct) if direction=="LONG" else entry_px*(1-tp_pct)
        sl_px = entry_px*(1-SL)     if direction=="LONG" else entry_px*(1+SL)
        mae=0.0; mfe=0.0
        exit_dt=valid_until; exit_px=entry_px; reason="expiry"
        for dt,px in hours:
            move = (px/entry_px-1.0) if direction=="LONG" else (entry_px/px-1.0)
            mfe = max(mfe, move)
            mae = min(mae, move)
            if (direction=="LONG" and px>=tp_px) or (direction=="SHORT" and px<=tp_px):
                exit_dt, exit_px, reason = dt, px, "tp"; break
            if (direction=="LONG" and px<=sl_px) or (direction=="SHORT" and px>=sl_px):
                exit_dt, exit_px, reason = dt, px, "sl"; break
        roi = (exit_px/entry_px-1.0) if direction=="LONG" else (entry_px/exit_px-1.0)
        return {
            "exit_dt":exit_dt, "exit_px":exit_px, "reason":reason,
            "roi":roi, "mfe_pct":mfe*100.0, "mae_pct":mae*100.0
        }

    priority = {"BTC":0,"ETH":1,"SOL":2}

    for day in cal:
        # If we have an open trade that exits *on or before* this day, settle it first
        if in_trade and in_trade["exit_dt"] <= day:
            settle(in_trade)
            in_trade=None

        # If flat, check today's signals (BTC > ETH > SOL).  
        # If a signal fires and we’re flat, open immediately.
        if in_trade is None:
            todays=[]
            for sym in ("BTC","ETH","SOL"):
                sig = day_signal(sym, day)
                if sig:
                    direction, entry_px, entry_dt = sig
                    todays.append((sym, direction, entry_px, entry_dt))
            if todays:
                todays.sort(key=lambda x: priority[x[0]])
                sym, direction, entry_px, entry_dt = todays[0]
                tp_pct = adaptive_tp(sym, dict(COINS)[f"{sym}USDT" if not sym.endswith("T") else sym]) if False else adaptive_tp(sym, next(tp for s,sy,tp in COINS if sy==sym))
                # simulate exit now → we’ll know when it frees up; also compute TP/SL/MAE/MFE/ROI
                sim = simulate_exit(sym, direction, entry_dt, entry_px, tp_pct)
                in_trade = {
                    "sym":sym, "direction":direction,
                    "entry_dt":entry_dt, "entry_px":entry_px,
                    "tp_pct":tp_pct, **sim
                }

        # If trade exits *during* this same day (exit_dt <= day), we already settled above
        # If it exits *after* today's close, we'll settle on a later iteration.
        # If it exits *earlier than today but we hadn’t settled yet* (edge case), handled at loop top.

        # Allow same-day re-entry: if trade just exited earlier today and we’re flat now, process signals again.
        if in_trade and in_trade["exit_dt"] <= day:
            settle(in_trade); in_trade=None
            # re-check signals today
            todays=[]
            for sym in ("BTC","ETH","SOL"):
                sig = day_signal(sym, day)
                if sig:
                    direction, entry_px, entry_dt = sig
                    todays.append((sym, direction, entry_px, entry_dt))
            if todays:
                todays.sort(key=lambda x: priority[x[0]])
                sym, direction, entry_px, entry_dt = todays[0]
                tp_pct = adaptive_tp(sym, next(tp for s,sy,tp in COINS if sy==sym))
                sim = simulate_exit(sym, direction, entry_dt, entry_px, tp_pct)
                in_trade = {"sym":sym,"direction":direction,"entry_dt":entry_dt,"entry_px":entry_px,"tp_pct":tp_pct, **sim}

    # Final settle if still open
    if in_trade:
        settle(in_trade)

    # -------- Output --------
    print("\n=== Backtest (current algo) — single bankroll, compounded ===")
    print(f"Start balance: $100.00")
    print(f"{'#':>3}  {'SYM':<3}  {'Dir':<5}  {'Entry UTC':<16}  {'Exit UTC':<16}  "
          f"{'TP%':>6}  {'MAE%':>7}  {'MFE%':>7}  {'ROI%':>7}  {'After$':>10}  {'Exit'}")
    for i,tr in enumerate(trades,1):
        print(f"{i:>3}  {tr['sym']:<3}  {tr['direction']:<5}  "
              f"{tr['entry_dt'].strftime('%Y-%m-%d %H:%M'):16}  "
              f"{tr['exit_dt'].strftime('%Y-%m-%d %H:%M'):16}  "
              f"{tr['tp_pct']*100:6.2f}  {tr['mae_pct']:7.2f}  {tr['mfe_pct']:7.2f}  "
              f"{tr['roi']*100:7.2f}  {tr['after_bal']:10.2f}  {tr['reason']}")
    if trades:
        total_roi = trades[-1]["after_bal"]/100.0
        maes=[t["mae_pct"] for t in trades]; mfes=[t["mfe_pct"] for t in trades]; rois=[t["roi"]*100 for t in trades]
        print("\nSummary:")
        print(f"Trades: {len(trades)}  |  Final equity: {total_roi:.2f}×  (${trades[-1]['after_bal']:.2f})")
        print(f"Avg MAE: {mean(maes):.2f}%  |  Med MAE: {median(maes):.2f}%")
        print(f"Avg MFE: {mean(mfes):.2f}%  |  Med MFE: {median(mfes):.2f}%")
        print(f"Avg ROI per trade: {mean(rois):.2f}%")
    else:
        print("\nNo trades in range.")
        
if __name__ == "__main__":
    run()
