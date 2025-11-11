#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Extreme outlier reversal backtest (hourly), with robust CCXT fetching, caching, and retries.
- No leverage (fees not leveraged)
- No same-bar exits
- ATR-based SL/TP (grid search)
- Optional dynamic scaling of z-thresholds by volatility regime
- Cross-confirmation (e.g., ETH/SOL require BTC to be opposite/extreme)
- Multi-exchange CCXT fallback (OKX -> Bybit -> Coinbase)
- Local parquet cache to speed up CI runs
"""

import argparse
import os
import sys
import time
import math
import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional, Tuple

import numpy as np
import pandas as pd

# ---- Constants & small utils -------------------------------------------------

UTC = timezone.utc
CACHE_DIR = os.environ.get("CANDLE_CACHE_DIR", ".cache")
os.makedirs(CACHE_DIR, exist_ok=True)

def parse_iso_date(s: str) -> datetime:
    if s.lower() == "now":
        return datetime.now(UTC)
    return datetime.strptime(s, "%Y-%m-%d").replace(tzinfo=UTC)

def to_ms(dt: datetime) -> int:
    return int(dt.timestamp() * 1000)

def human_dt(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, tz=UTC).isoformat()

def log(msg: str) -> None:
    print(msg, flush=True)

# ---- Exchange client (ccxt) --------------------------------------------------

def make_ccxt_client(ex_name: str, timeout_ms: int):
    import ccxt
    cls = getattr(ccxt, ex_name)
    return cls({
        "enableRateLimit": True,
        "timeout": timeout_ms,
    })

def ccxt_name_for_symbol(ex: str, sym: str) -> str:
    # We use USDT perpetual spot tickers. For CCXT spot tickers:
    # OKX/Bybit/Coinbase use "BTC/USDT", "ETH/USDT", "SOL/USDT".
    base = sym.upper().replace("USDT", "")
    return f"{base}/USDT"

def parquet_path(symbol: str, start: datetime, end: datetime, ex: str) -> str:
    return os.path.join(
        CACHE_DIR,
        f"{symbol}_{ex}_{start.date()}_{end.date()}_1h.parquet"
    )

def safe_sleep(seconds: float):
    try:
        time.sleep(seconds)
    except KeyboardInterrupt:
        raise

def fetch_ccxt_ohlcv_paginated(
    ex,
    market: str,
    timeframe: str,
    since_ms: int,
    until_ms: int,
    limit_per_call: int = 1000,
    max_retries: int = 6,
    backoff_base: float = 0.9,
) -> List[List[float]]:
    """Fetch OHLCV by walking forward with retries/backoff."""
    out: List[List[float]] = []
    cursor = since_ms
    last_ts = None
    while True:
        if cursor >= until_ms:
            break

        for attempt in range(max_retries):
            try:
                # Some exchanges support "until"; ccxt fetchOHLCV signature is (symbol,timeframe,since,limit,params)
                rows = ex.fetch_ohlcv(market, timeframe=timeframe, since=cursor, limit=limit_per_call)
                if not rows:
                    # Avoid infinite loops
                    cursor += 60 * 60 * 1000
                    break
                # ensure strictly increasing and within range
                dedup = []
                for r in rows:
                    ts = int(r[0])
                    if ts <= (last_ts or -1):
                        continue
                    if ts > until_ms:
                        break
                    dedup.append(r)
                    last_ts = ts
                out.extend(dedup)
                if len(rows) < limit_per_call:
                    # likely reached end
                    cursor = (last_ts or cursor) + 1
                else:
                    cursor = (last_ts or cursor) + 1
                # small pacing to be nice
                safe_sleep(0.05)
                break
            except Exception as e:
                # request timeout / DDoS / transient => backoff & retry
                if attempt == max_retries - 1:
                    raise
                delay = (2 ** attempt) * backoff_base
                safe_sleep(delay)
        # loop continues until end
    return out

def fetch_multi_exchange_hourly(
    symbol: str,
    start_dt: datetime,
    end_dt: datetime,
    exchanges: List[str],
    timeout_ms: int,
    limit_per_call: int,
    use_cache: bool,
) -> pd.DataFrame:
    """Try exchanges in order; cache by exchange for reproducibility."""
    errs = []
    for ex_name in exchanges:
        cache_file = parquet_path(symbol, start_dt, end_dt, ex_name)
        if use_cache and os.path.isfile(cache_file):
            df = pd.read_parquet(cache_file)
            # guard against empty cache
            if not df.empty:
                return df

        try:
            import ccxt  # ensure available before constructing
            ex = make_ccxt_client(ex_name, timeout_ms)
            mkt = ccxt_name_for_symbol(ex_name, symbol)
            # load markets for robust symbol mapping
            ex.load_markets()
            if mkt not in ex.markets:
                # Try alternate symbol resolutions
                # e.g., Coinbase might use USDT but sometimes liquidity is on USD.
                alt = mkt.replace("/USDT", "/USD")
                if alt in ex.markets:
                    mkt = alt
            log(f"  [{ex_name}] {symbol} 1h {start_dt.isoformat()} → {end_dt.isoformat()}")
            rows = fetch_ccxt_ohlcv_paginated(
                ex, mkt, "1h", to_ms(start_dt), to_ms(end_dt),
                limit_per_call=limit_per_call
            )
            if not rows:
                raise RuntimeError(f"{ex_name} returned no OHLCV rows.")
            df = pd.DataFrame(rows, columns=["ts","open","high","low","close","volume"])
            df["ts"] = pd.to_datetime(df["ts"], unit="ms", utc=True)
            df = df.set_index("ts").sort_index()
            # enforce complete hourly alignment (optional)
            df = df[~df.index.duplicated(keep="first")]
            if not df.empty and use_cache:
                df.to_parquet(cache_file)
            return df
        except Exception as e:
            errs.append(f"{ex_name}: {repr(e)}")
            log(f"    {ex_name} failed: {e}; trying next exchange...")
            continue
    raise RuntimeError("All exchanges failed. Errors: " + " | ".join(errs))

# ---- Indicators & signals ----------------------------------------------------

def rolling_atr(df: pd.DataFrame, lookback: int) -> pd.Series:
    high = df["high"].astype(float)
    low = df["low"].astype(float)
    close = df["close"].astype(float)
    prev_close = close.shift(1)
    tr = pd.concat([
        (high - low),
        (high - prev_close).abs(),
        (low - prev_close).abs(),
    ], axis=1).max(axis=1)
    return tr.rolling(lookback, min_periods=lookback).mean()

def rolling_zscore(x: pd.Series, lookback: int) -> pd.Series:
    r = x - x.rolling(lookback).mean()
    std = x.rolling(lookback).std(ddof=0)
    return r / std

@dataclass
class DynParams:
    z_thresh: float
    scale_lo: float
    scale_hi: float
    regime_lookback: int
    vol_pctl_min: Optional[float]  # e.g. 20 (percentile) or None

def build_signals(
    df: pd.DataFrame,
    zlk: int,
    dyn: DynParams,
    utc_hours: Optional[List[int]],
    side: str = "both",
) -> pd.DataFrame:
    """Create extreme-outlier entry intent (not applying confirm yet)."""
    out = df.copy()
    out["ret1"] = np.log(out["close"]).diff()
    out["z"] = rolling_zscore(out["ret1"], zlk)

    # Volatility regime via rolling std percentile
    if dyn.vol_pctl_min is not None:
        vol = out["ret1"].rolling(dyn.regime_lookback).std(ddof=0)
        # Percentile filter: compute rolling rank vs trailing dist approx using expanding percentile
        # Cheap proxy: normalize by expanding max; still useful as a low-pass filter.
        pctl = (vol / vol.expanding().max()).clip(0, 1) * 100.0
        out["in_regime"] = pctl >= float(dyn.vol_pctl_min)
    else:
        out["in_regime"] = True

    # Dynamic scaling: scale threshold by ATR regime (simple proxy using rolling std of returns)
    scale = out["ret1"].rolling(dyn.regime_lookback).std(ddof=0)
    scale_norm = (scale / scale.expanding().median().fillna(method="bfill")).clip(dyn.scale_lo, dyn.scale_hi)
    out["z_dyn"] = out["z"] / scale_norm

    # Entry intents
    zt = float(dyn.z_thresh)
    go_long  = (out["z_dyn"] <= -zt)
    go_short = (out["z_dyn"] >=  zt)

    if side == "long":
        go_short = pd.Series(False, index=out.index)
    elif side == "short":
        go_long  = pd.Series(False, index=out.index)

    out["want_long"]  = go_long & out["in_regime"]
    out["want_short"] = go_short & out["in_regime"]

    # Restrict to certain UTC hours if provided
    if utc_hours:
        hh = out.index.tz_convert(UTC).hour
        mask = hh.isin(utc_hours)
        out["want_long"]  &= mask
        out["want_short"] &= mask

    return out

def apply_cross_confirm(
    intents: Dict[str, pd.DataFrame],
    confirm_map: Dict[str, str],
    mode: str,
    z_needed: float
) -> None:
    """
    Modify intents in place using confirmation rules.
    mode: "opposite" => e.g., if ETH wants long, require BTC z_dyn >= +z_needed
          "same"     => e.g., if ETH wants long, require BTC z_dyn <= -z_needed
    """
    for sym, ref in confirm_map.items():
        if sym not in intents or ref not in intents:
            continue
        A = intents[sym]
        B = intents[ref]
        if mode == "opposite":
            A["want_long"]  &= B["z_dyn"] >= +z_needed
            A["want_short"] &= B["z_dyn"] <= -z_needed
        elif mode == "same":
            A["want_long"]  &= B["z_dyn"] <= -z_needed
            A["want_short"] &= B["z_dyn"] >= +z_needed

# ---- Backtest core -----------------------------------------------------------

@dataclass
class Trade:
    entry_time: pd.Timestamp
    entry: float
    side: str  # "long" or "short"
    sl: float
    tp: float
    exit_time: Optional[pd.Timestamp] = None
    exit: Optional[float] = None
    r: Optional[float] = None

def backtest_atr_sl_tp(
    df: pd.DataFrame,
    intents: pd.DataFrame,
    atr_lookback: int,
    sl_mult: float,
    tp_mult: float,
    fee_bps: float,
) -> Tuple[List[Trade], float]:
    """
    No leverage; fees unlevered; no same-bar exits (evaluate from next bar).
    """
    atr = rolling_atr(df, atr_lookback)
    close = df["close"].astype(float)
    high  = df["high"].astype(float)
    low   = df["low"].astype(float)

    trades: List[Trade] = []
    equity = 100.0

    pos: Optional[Trade] = None

    for i in range(len(df) - 1):
        t  = df.index[i]
        t1 = df.index[i + 1]  # next bar

        if pos is None:
            # consider entries at close[t] if intent true at t; exits only start at t1
            if intents["want_long"].iloc[i]:
                a = atr.iloc[i]
                if np.isfinite(a) and a > 0:
                    e = close.iloc[i]
                    sl = e - sl_mult * a
                    tp = e + tp_mult * a
                    pos = Trade(entry_time=t, entry=e, side="long", sl=sl, tp=tp)
            elif intents["want_short"].iloc[i]:
                a = atr.iloc[i]
                if np.isfinite(a) and a > 0:
                    e = close.iloc[i]
                    sl = e + sl_mult * a
                    tp = e - tp_mult * a
                    pos = Trade(entry_time=t, entry=e, side="short", sl=sl, tp=tp)
        else:
            # manage from next bar t1 (no same-bar exits)
            hi = high.iloc[i + 1]
            lo = low.iloc[i + 1]
            ex_price = None
            if pos.side == "long":
                # SL first touch priority same-bar? we’re on next bar anyway; check both
                if lo <= pos.sl:
                    ex_price = pos.sl
                if ex_price is None and hi >= pos.tp:
                    ex_price = pos.tp
            else:
                if hi >= pos.sl:
                    ex_price = pos.sl
                if ex_price is None and lo <= pos.tp:
                    ex_price = pos.tp

            if ex_price is not None:
                # round trip fees: entry + exit
                fee = (fee_bps / 10000.0)
                raw_r = (ex_price - pos.entry) / pos.entry if pos.side == "long" else (pos.entry - ex_price) / pos.entry
                net_r = raw_r - (2 * fee)
                pos.exit_time = t1
                pos.exit = ex_price
                pos.r = net_r * 100.0  # percentage
                equity *= (1.0 + net_r)
                trades.append(pos)
                pos = None

    return trades, equity

def summarize_trades(trades: List[Trade]) -> Dict[str, float]:
    if not trades:
        return dict(trades=0, win=0.0, avg=0.0, med=0.0)
    rs = np.array([t.r for t in trades if t.r is not None], dtype=float)
    wins = (rs > 0).mean() * 100.0
    return dict(trades=len(rs), win=wins, avg=float(np.mean(rs)), med=float(np.median(rs)))

# ---- CLI / main --------------------------------------------------------------

def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Extreme outlier hourly reversal backtest (robust).")
    p.add_argument("--symbols", type=str, default="BTCUSDT,ETHUSDT,SOLUSDT")
    p.add_argument("--start", type=str, default="2024-01-01")
    p.add_argument("--end", type=str, default="now")
    p.add_argument("--fee-bps", type=float, default=8.0)
    p.add_argument("--no-leverage", action="store_true", default=True)
    p.add_argument("--utc-hours", type=str, default="13,14,15,16,17,18,19,20")
    p.add_argument("--side", type=str, choices=["both","long","short"], default="both")

    # signal/dynamic
    p.add_argument("--z-lookback", type=int, default=96)
    p.add_argument("--z-thresh", type=float, default=2.5)
    p.add_argument("--dyn-scale-lo", type=float, default=0.8)
    p.add_argument("--dyn-scale-hi", type=float, default=1.6)
    p.add_argument("--regime-lookback", type=int, default=336)
    p.add_argument("--vol-pctl-min", type=float, default=None)

    # cross confirm
    p.add_argument("--confirm-map", type=str, default="SOLUSDT:BTCUSDT,ETHUSDT:BTCUSDT")
    p.add_argument("--confirm-mode", type=str, choices=["opposite","same"], default="opposite")
    p.add_argument("--confirm-z", type=float, default=2.5)

    # risk mgmt
    p.add_argument("--atr-lookback", type=int, default=48)
    p.add_argument("--sl-list", type=str, default="2.0,1.5,1.0,0.75")
    p.add_argument("--tp-list", type=str, default="2.0,1.5")

    # engine limits
    p.add_argument("--exchanges", type=str, default="okx,bybit,coinbase")
    p.add_argument("--ccxt-timeout-ms", type=int, default=20000)
    p.add_argument("--limit-per-call", type=int, default=1000)
    p.add_argument("--use-cache", action="store_true", default=True)
    p.add_argument("--max-bars", type=int, default=0, help="0 = no cap")
    p.add_argument("--max-symbols", type=int, default=0, help="0 = all")
    return p.parse_args()

def main():
    args = parse_args()
    syms = [s.strip().upper() for s in args.symbols.split(",") if s.strip()]
    if args.max_symbols and len(syms) > args.max_symbols:
        syms = syms[:args.max_symbols]

    start_dt = parse_iso_date(args.start)
    end_dt   = parse_iso_date(args.end)
    if end_dt <= start_dt:
        raise ValueError("end must be after start")

    utc_hours = [int(h) for h in args.utc_hours.split(",")] if args.utc_hours else []
    exchanges = [x.strip() for x in args.exchanges.split(",") if x.strip()]

    log("Running extreme outlier backtest with refinements...")
    log(f"Range: {start_dt.isoformat()} → {end_dt.isoformat()}  (UTC)")
    log(f"Exchanges: {','.join(exchanges)}  Fee: {args.fee_bps} bps/side  Side: {args.side}")
    log(f"UTC hours: {utc_hours}")
    log(f"Regime filter: vol_pctl_min={args.vol_pctl_min} lookback={args.regime_lookback}")
    log(f"Dynamic: z_ref={args.z_thresh} scale[{args.dyn_scale_lo},{args.dyn_scale_hi}]")
    log(f"Cross-confirm map: {args.confirm_map or '{}'}   mode={args.confirm_mode}  z={args.confirm_z}")
    log("No leverage. No same-bar exits. ATR SL/TP. CCXT only.")

    # Preload confirm symbols once
    confirm_pairs = {}
    if args.confirm_map:
        for kv in args.confirm_map.split(","):
            a, b = kv.split(":")
            confirm_pairs[a.strip().upper()] = b.strip().upper()

    all_data: Dict[str, pd.DataFrame] = {}
    all_intents: Dict[str, pd.DataFrame] = {}
    results: Dict[str, Dict[str, float]] = {}
    grid_rows: Dict[str, List[Tuple[float,float,float,int,float]]] = {}

    # Preload any confirm reference symbols (to avoid duplicate fetches per target)
    confirm_refs = sorted({v for v in confirm_pairs.values()})
    preload_syms = list(dict.fromkeys(confirm_refs + syms))  # preserve order, unique

    # Fetch & build intents
    for idx, s in enumerate(preload_syms, 1):
        try:
            log(f"=== {s} ===")
            df = fetch_multi_exchange_hourly(
                s, start_dt, end_dt,
                exchanges=exchanges,
                timeout_ms=args.ccxt_timeout_ms,
                limit_per_call=args.limit_per_call,
                use_cache=args.use_cache,
            )
            if args.max-bars if False else False:  # placeholder to avoid linter (not used)
                pass
            if args.max_bars and len(df) > args.max_bars:
                df = df.iloc[-args.max_bars:].copy()

            all_data[s] = df
            dyn = DynParams(
                z_thresh=args.z_thresh,
                scale_lo=args.dyn_scale_lo,
                scale_hi=args.dyn_scale_hi,
                regime_lookback=args.regime_lookback,
                vol_pctl_min=args.vol_pctl_min
            )
            intents = build_signals(df, args.z_lookback, dyn, utc_hours, side=args.side)
            all_intents[s] = intents
            log(f"  got {len(df):,} bars {df.index[0]} → {df.index[-1]}")
        except Exception as e:
            log(f"  Data fetch failed for {s}: {e}")

    # Cross-confirmation pass (in place)
    if confirm_pairs:
        apply_cross_confirm(all_intents, confirm_pairs, args.confirm_mode, float(args.confirm_z))

    # Grid over SL/TP
    sls = [float(x) for x in args.sl_list.split(",") if x.strip()]
    tps = [float(x) for x in args.tp_list.split(",") if x.strip()]

    sum_rows = []
    for s in syms:
        if s not in all_data or s not in all_intents:
            log(f"=== {s} ===\n  (skipped; no data)")
            continue
        df = all_data[s]
        intents = all_intents[s]

        best_eq = -1.0
        best_pair = (None, None)
        grid = []
        for sl in sls:
            for tp in tps:
                trades, eq = backtest_atr_sl_tp(
                    df, intents, args.atr_lookback, sl, tp, args.fee_bps
                )
                summ = summarize_trades(trades)
                grid.append((sl, tp, eq, summ["trades"], summ["win"]))
                if eq > best_eq:
                    best_eq = eq
                    best_pair = (sl, tp)
        grid_rows[s] = grid

        # Re-run for best pair to report full stats
        if best_pair[0] is not None:
            trades, eq = backtest_atr_sl_tp(
                df, intents, args.atr_lookback, best_pair[0], best_pair[1], args.fee_bps
            )
            summ = summarize_trades(trades)
            log(f"\n=== {s} (optimal) ===")
            log(f"  SL x ATR = {best_pair[0]:.2f}, TP x ATR = {best_pair[1]:.2f}")
            log(f"  Trades: {summ['trades']:,}, Win%: {summ['win']:.1f}%, AvgR: {summ['avg']:.2f}%, MedR: {summ['med']:.2f}%")
            log(f"  Final equity: ${eq:.2f}")

            sum_rows.append(dict(
                Symbol=s,
                Trades=summ["trades"],
                WinPct=round(summ["win"], 2),
                AvgR=round(summ["avg"], 4),
                MedR=round(summ["med"], 4),
                FinalEq=round(eq, 2),
                Best=f"{best_pair[0]:.2f}/{best_pair[1]:.2f}",
            ))

            # print compact grid
            if grid:
                hdr = "|   S
