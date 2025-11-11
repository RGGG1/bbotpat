#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Extreme outlier reversal backtest (hourly)
- Robust CCXT data (OKX → Bybit → Coinbase) with caching & retry
- ATR-based SL/TP, no leverage, no same-bar exits
- Dynamic z-threshold scaling and volatility regime filters
- Cross-confirmation (BTC reference)
"""

import argparse, os, sys, time, math, json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional, Tuple
import numpy as np
import pandas as pd

UTC = timezone.utc
CACHE_DIR = ".cache"
os.makedirs(CACHE_DIR, exist_ok=True)

# ───────────────────────────────────────────────
# Basic utilities
# ───────────────────────────────────────────────
def parse_iso_date(s: str) -> datetime:
    if s.lower() == "now":
        return datetime.now(UTC)
    return datetime.strptime(s, "%Y-%m-%d").replace(tzinfo=UTC)

def to_ms(dt: datetime) -> int:
    return int(dt.timestamp() * 1000)

def log(msg: str): print(msg, flush=True)

# ───────────────────────────────────────────────
# CCXT multi-exchange fetching (OKX→Bybit→Coinbase)
# ───────────────────────────────────────────────
def make_ccxt_client(ex_name: str, timeout_ms: int):
    import ccxt
    cls = getattr(ccxt, ex_name)
    return cls({"enableRateLimit": True, "timeout": timeout_ms})

def fetch_ccxt_ohlcv(ex, market: str, timeframe: str, since_ms: int, until_ms: int, limit: int) -> list:
    out = []
    cursor = since_ms
    last = None
    while True:
        if cursor >= until_ms: break
        rows = ex.fetch_ohlcv(market, timeframe=timeframe, since=cursor, limit=limit)
        if not rows: break
        for r in rows:
            ts = int(r[0])
            if last and ts <= last: continue
            if ts > until_ms: break
            out.append(r); last = ts
        if len(rows) < limit: break
        cursor = last + 1
        time.sleep(0.05)
    return out

def fetch_multi(symbol, start, end, exchanges, timeout_ms=20000, limit=1000):
    import ccxt
    for ex_name in exchanges:
        cache = os.path.join(CACHE_DIR, f"{symbol}_{ex_name}_{start.date()}_{end.date()}_1h.parquet")
        if os.path.isfile(cache):
            df = pd.read_parquet(cache)
            if not df.empty:
                return df
        try:
            ex = make_ccxt_client(ex_name, timeout_ms)
            ex.load_markets()
            market = f"{symbol.replace('USDT','')}/USDT"
            if market not in ex.markets:
                alt = f"{symbol.replace('USDT','')}/USD"
                if alt in ex.markets: market = alt
            log(f"[{ex_name}] {symbol} 1h fetch {start}→{end}")
            rows = fetch_ccxt_ohlcv(ex, market, "1h", to_ms(start), to_ms(end), limit)
            if not rows: raise RuntimeError("No data")
            df = pd.DataFrame(rows, columns=["ts","open","high","low","close","volume"])
            df["ts"] = pd.to_datetime(df["ts"], unit="ms", utc=True)
            df = df.set_index("ts").sort_index()
            df.to_parquet(cache)
            return df
        except Exception as e:
            log(f"  {ex_name} failed: {e}")
    raise RuntimeError(f"All exchanges failed for {symbol}")

# ───────────────────────────────────────────────
# Indicators
# ───────────────────────────────────────────────
def rolling_atr(df, lookback):
    c, h, l = df["close"], df["high"], df["low"]
    pc = c.shift(1)
    tr = pd.concat([(h-l), (h-pc).abs(), (l-pc).abs()], axis=1).max(axis=1)
    return tr.rolling(lookback, min_periods=lookback).mean()

def rolling_z(x, lookback):
    return (x - x.rolling(lookback).mean()) / x.rolling(lookback).std(ddof=0)

@dataclass
class DynParams:
    z_thresh: float
    scale_lo: float
    scale_hi: float
    regime_lookback: int
    vol_pctl_min: Optional[float]

def build_signals(df, zlk, dyn: DynParams, utc_hours: List[int], side="both"):
    out = df.copy()
    out["ret1"] = np.log(out["close"]).diff()
    out["z"] = rolling_z(out["ret1"], zlk)
    if dyn.vol_pctl_min:
        vol = out["ret1"].rolling(dyn.regime_lookback).std(ddof=0)
        pctl = (vol / vol.expanding().max()).clip(0,1)*100
        out["in_regime"] = pctl >= dyn.vol_pctl_min
    else:
        out["in_regime"] = True
    scale = out["ret1"].rolling(dyn.regime_lookback).std(ddof=0)
    norm = (scale / scale.expanding().median()).clip(dyn.scale_lo, dyn.scale_hi)
    out["z_dyn"] = out["z"]/norm
    zt = dyn.z_thresh
    go_long = (out["z_dyn"] <= -zt)
    go_short = (out["z_dyn"] >= zt)
    if side=="long": go_short[:] = False
    if side=="short": go_long[:] = False
    hh = out.index.tz_convert(UTC).hour
    mask = hh.isin(utc_hours)
    out["want_long"] = go_long & mask & out["in_regime"]
    out["want_short"] = go_short & mask & out["in_regime"]
    return out

def apply_cross_confirm(intents, confirm_map, mode, z_needed):
    for sym, ref in confirm_map.items():
        if sym not in intents or ref not in intents: continue
        A,B = intents[sym], intents[ref]
        if mode=="opposite":
            A["want_long"] &= B["z_dyn"]>=+z_needed
            A["want_short"]&= B["z_dyn"]<=-z_needed
        elif mode=="same":
            A["want_long"] &= B["z_dyn"]<=-z_needed
            A["want_short"]&= B["z_dyn"]>=+z_needed

# ───────────────────────────────────────────────
# Backtest core
# ───────────────────────────────────────────────
@dataclass
class Trade:
    entry_time: pd.Timestamp
    entry: float
    side: str
    sl: float
    tp: float
    exit_time: Optional[pd.Timestamp]=None
    exit: Optional[float]=None
    r: Optional[float]=None

def backtest(df, intents, atr_lb, slx, tpx, fee_bps):
    atr = rolling_atr(df, atr_lb)
    c,h,l = df["close"], df["high"], df["low"]
    pos=None; equity=100.0; trades=[]
    for i in range(len(df)-1):
        if pos is None:
            if intents["want_long"].iloc[i]:
                a=atr.iloc[i]; e=c.iloc[i]
                pos=Trade(df.index[i],e,"long",e-slx*a,e+tpx*a)
            elif intents["want_short"].iloc[i]:
                a=atr.iloc[i]; e=c.iloc[i]
                pos=Trade(df.index[i],e,"short",e+slx*a,e-tpx*a)
        else:
            hi,lo=h.iloc[i+1],l.iloc[i+1]
            ex=None
            if pos.side=="long":
                if lo<=pos.sl: ex=pos.sl
                elif hi>=pos.tp: ex=pos.tp
            else:
                if hi>=pos.sl: ex=pos.sl
                elif lo<=pos.tp: ex=pos.tp
            if ex:
                fee=(fee_bps/10000)
                raw=(ex-pos.entry)/pos.entry if pos.side=="long" else (pos.entry-ex)/pos.entry
                net=raw-2*fee
                pos.exit_time=df.index[i+1];pos.exit=ex;pos.r=net*100
                equity*=(1+net); trades.append(pos); pos=None
    return trades,equity

def summarize(trades):
    if not trades: return dict(trades=0,win=0,avg=0,med=0)
    r=np.array([t.r for t in trades]); return dict(
        trades=len(r),win=(r>0).mean()*100,avg=r.mean(),med=np.median(r)
    )

# ───────────────────────────────────────────────
# Main
# ───────────────────────────────────────────────
def main():
    ap=argparse.ArgumentParser()
    ap.add_argument("--symbols",default="BTCUSDT,ETHUSDT,SOLUSDT")
    ap.add_argument("--start",default="2024-01-01")
    ap.add_argument("--end",default="now")
    ap.add_argument("--fee-bps",type=float,default=8.0)
    ap.add_argument("--utc-hours",default="13,14,15,16,17,18,19,20")
    ap.add_argument("--side",default="both")
    ap.add_argument("--z-thresh",type=float,default=2.5)
    ap.add_argument("--z-lookback",type=int,default=96)
    ap.add_argument("--atr-lookback",type=int,default=48)
    ap.add_argument("--sl-list",default="2.0,1.5,1.0,0.75")
    ap.add_argument("--tp-list",default="2.0,1.5")
    ap.add_argument("--confirm-map",default="SOLUSDT:BTCUSDT,ETHUSDT:BTCUSDT")
    ap.add_argument("--confirm-mode",default="opposite")
    args=ap.parse_args()

    syms=[s.strip().upper() for s in args.symbols.split(",")]
    start=parse_iso_date(args.start); end=parse_iso_date(args.end)
    utc_hours=[int(h) for h in args.utc_hours.split(",")]

    log(f"Running extreme-outlier backtest {start}→{end}")

    exch=["okx","bybit","coinbase"]
    data={}; intents={}
    for s in syms:
        try:
            df=fetch_multi(s,start,end,exch)
            data[s]=df
            dyn=DynParams(args.z_thresh,0.8,1.6,336,None)
            intents[s]=build_signals(df,args.z_lookback,dyn,utc_hours,args.side)
            log(f"  got {len(df)} bars for {s}")
        except Exception as e:
            log(f"  failed {s}: {e}")

    confirm={a:b for a,b in [x.split(":") for x in args.confirm-map.split(",") if ":" in x]}
    apply_cross_confirm(intents,confirm,args.confirm_mode,float(args.z_thresh))

    sls=[float(x) for x in args.sl_list.split(",")]
    tps=[float(x) for x in args.tp_list.split(",")]
    sumrows=[]
    for s in syms:
        if s not in data: continue
        best_eq=-1; best=(None,None)
        grid=[]
        for sl in sls:
            for tp in tps:
                trades,eq=backtest(data[s],intents[s],args.atr_lookback,sl,tp,args.fee_bps)
                summ=summarize(trades)
                grid.append((sl,tp,eq,summ["trades"],summ["win"]))
                if eq>best_eq: best_eq=eq; best=(sl,tp)
        log(f"\n=== {s} (optimal) ===")
        log(f"  SLxATR={best[0]}, TPxATR={best[1]}")
        trades,eq=backtest(data[s],intents[s],args.atr_lookback,best[0],best[1],args.fee_bps)
        summ=summarize(trades)
        log(f"  Trades:{summ['trades']}, Win%:{summ['win']:.1f}%, AvgR:{summ['avg']:.2f}%, MedR:{summ['med']:.2f}%, Eq:${eq:.2f}")
        hdr = "|   SLxATR |   TPxATR |   FinalEq |   #Trades |   Win% |\n|----------|----------|-----------|-----------|--------|"
        log(hdr)
        for sl,tp,eqv,n,w in sorted(grid,key=lambda r:(-r[2],r[0],r[1]))[:10]:
            log(f"| {sl:>8} | {tp:>8} | {eqv:>9.2f} | {n:>9} | {w:>6.1f} |")
        sumrows.append(dict(Symbol=s,Trades=summ["trades"],WinPct=summ["win"],AvgR=summ["avg"],MedR=summ["med"],FinalEq=eq,Best=f"{best[0]}/{best[1]}"))
    if sumrows:
        log("\n=== Summary ===")
        df=pd.DataFrame(sumrows)
        print(df.to_string(index=False))

if __name__=="__main__":
    main()
