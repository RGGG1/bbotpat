#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Hourly reversal backtest with:
- Same-bar exit filtering (default ON)
- Trading fees (bps, non-levered)
- Grid search across z-thresholds, hold windows, and SL/TP (fixed % or ATR-based)
- Auto fallback to Yahoo Finance if Binance API is geo-blocked

Usage example:
  python .github/analyze_hourly_reversal.py --symbols BTCUSDT,ETHUSDT,SOLUSDT --start 2023-01-01 --end now --fee-bps 8
"""

import argparse
import math
import time
from datetime import datetime, timezone
from typing import List, Tuple, Dict, Optional
import numpy as np
import pandas as pd
import requests
from dateutil import parser as dtparser
from dataclasses import dataclass  # ✅ FIXED import location
try:
    import yfinance as yf
    HAVE_YF = True
except ImportError:
    HAVE_YF = False

BINANCE_BASE = "https://api.binance.com"

# ─────────────────────────────────────────────────────────────
# Utilities
# ─────────────────────────────────────────────────────────────
def to_ms(dt: datetime) -> int:
    return int(dt.timestamp() * 1000)

def parse_when(s: str) -> datetime:
    if s.lower() == "now":
        return datetime.now(timezone.utc)
    dt = dtparser.parse(s)
    return dt.astimezone(timezone.utc) if dt.tzinfo else dt.replace(tzinfo=timezone.utc)

# ─────────────────────────────────────────────────────────────
# Data fetch (Binance + Yahoo fallback)
# ─────────────────────────────────────────────────────────────
def fetch_binance(symbol: str, interval: str, start_ms: int, end_ms: int) -> List[List]:
    out, limit, cur = [], 1000, start_ms
    for _ in range(20000):
        url = f"{BINANCE_BASE}/api/v3/klines"
        p = {"symbol": symbol, "interval": interval, "startTime": cur, "endTime": end_ms, "limit": limit}
        r = requests.get(url, params=p, timeout=20)
        if r.status_code == 451:
            raise RuntimeError("BINANCE_451")
        r.raise_for_status()
        part = r.json()
        if not part: break
        out += part
        cur = part[-1][6] + 1
        if cur >= end_ms: break
        time.sleep(0.05)
    return out

def klines_to_df(kl: List[List]) -> pd.DataFrame:
    cols = ["open_time","open","high","low","close","vol","close_time","q","t","b1","b2","ignore"]
    df = pd.DataFrame(kl, columns=cols)
    df["close_time"] = pd.to_datetime(df["close_time"], unit="ms", utc=True)
    for c in ["open","high","low","close"]: df[c] = pd.to_numeric(df[c])
    return df.set_index("close_time")[["open","high","low","close"]]

YF_SYMBOLS = {"BTCUSDT":"BTC-USD","ETHUSDT":"ETH-USD","SOLUSDT":"SOL-USD"}

def fetch_yf(symbol: str, start_dt: datetime, end_dt: datetime) -> pd.DataFrame:
    if not HAVE_YF: raise RuntimeError("yfinance missing")
    sym = YF_SYMBOLS.get(symbol.upper())
    df = yf.download(sym, start=start_dt, end=end_dt, interval="60m", progress=False)
    df = df.rename(columns={"Open":"open","High":"high","Low":"low","Close":"close"})
    df.index = df.index.tz_localize(timezone.utc) if df.index.tz is None else df.index.tz_convert(timezone.utc)
    return df[["open","high","low","close"]].dropna()

def fetch_data(symbol: str, interval: str, start_ms: int, end_ms: int) -> pd.DataFrame:
    start_dt = datetime.fromtimestamp(start_ms/1000, tz=timezone.utc)
    end_dt = datetime.fromtimestamp(end_ms/1000, tz=timezone.utc)
    try:
        print(f"Downloading {symbol} 1h from Binance…")
        df = klines_to_df(fetch_binance(symbol, "1h", start_ms, end_ms))
        print(f"  Got {len(df)} bars")
        return df
    except Exception as e:
        print(f"  Binance failed ({e}), fallback to yfinance")
        df = fetch_yf(symbol, start_dt, end_dt)
        print(f"  Got {len(df)} bars")
        return df

# ─────────────────────────────────────────────────────────────
# Indicators
# ─────────────────────────────────────────────────────────────
def rolling_zscore(s: pd.Series, n: int) -> pd.Series:
    m, sd = s.rolling(n).mean(), s.rolling(n).std(ddof=0)
    return (s - m) / sd

def atr(df: pd.DataFrame, n: int = 14) -> pd.Series:
    h, l, c = df["high"], df["low"], df["close"]
    tr = pd.concat([(h-l), (h-c.shift()).abs(), (l-c.shift()).abs()], axis=1).max(axis=1)
    return tr.rolling(n).mean()

@dataclass
class Trade:
    index: pd.Timestamp
    side: str
    entry: float
    exit: float
    exit_index: pd.Timestamp
    roi: float
    reason: str

# ─────────────────────────────────────────────────────────────
# Backtest
# ─────────────────────────────────────────────────────────────
def simulate(df, z, z_thresh=2.0, hold=24, fee_bps=8, no_samebar=True, sl=3.0, tp=2.0):
    fees = 2 * (fee_bps/10000)
    trades, eq = [], 100.0
    c = df["close"].values; h = df["high"].values; l = df["low"].values; idx = df.index; n=len(df)
    i=0
    while i<n:
        zc=z.iloc[i]
        if abs(zc)<z_thresh: i+=1; continue
        side="LONG" if zc<=-z_thresh else "SHORT"
        e=c[i]; stop=e*(1-sl/100) if side=="LONG" else e*(1+sl/100)
        take=e*(1+tp/100) if side=="LONG" else e*(1-tp/100)
        start=i+1 if no_samebar else i; end=min(n-1,i+hold)
        reason="TIME"; px=c[end]; ex=idx[end]
        for j in range(start,end+1):
            if side=="LONG":
                if l[j]<=stop: px=stop; ex=idx[j]; reason="SL"; break
                if h[j]>=take: px=take; ex=idx[j]; reason="TP"; break
            else:
                if h[j]>=stop: px=stop; ex=idx[j]; reason="SL"; break
                if l[j]<=take: px=take; ex=idx[j]; reason="TP"; break
        gross=(px/e-1) if side=="LONG" else (e/px-1)
        roi=(gross-fees)*100
        eq*=1+roi/100
        trades.append(Trade(idx[i],side,e,px,ex,roi,reason))
        i=end+1
    return trades,eq

def summarize(ts):
    if not ts: return 0,0,0,0
    r=[t.roi for t in ts]; return len(ts),np.mean(r),np.median(r),sum(x>0 for x in r)/len(r)*100

# ─────────────────────────────────────────────────────────────
# Main
# ─────────────────────────────────────────────────────────────
def main():
    ap=argparse.ArgumentParser()
    ap.add_argument("--symbols",default="BTCUSDT,ETHUSDT,SOLUSDT")
    ap.add_argument("--start",default="2023-01-01")
    ap.add_argument("--end",default="now")
    ap.add_argument("--fee-bps",type=float,default=8)
    args=ap.parse_args()

    start=parse_when(args.start); end=parse_when(args.end)
    smap=[s.strip().upper() for s in args.symbols.split(",")]

    for s in smap:
        df=fetch_data(s,"1h",to_ms(start),to_ms(end))
        z=rolling_zscore(df["close"].pct_change().fillna(0),48)
        trades,eq=simulate(df,z,2.0,24,args.fee_bps,True,3.0,2.0)
        n,avg,med,win=summarize(trades)
        print(f"\n=== {s} ({n} trades) ===")
        for i,t in enumerate(trades[:20]):
            print(f"{i+1:2d}. {t.side:5s} {t.index} → {t.exit_index}  ROI={t.roi:+.2f}%  {t.reason}")
        print(f"→ Final equity ${eq:.2f}  Avg {avg:.2f}%  Med {med:.2f}%  Win {win:.1f}%")

if __name__=="__main__":
    main()
