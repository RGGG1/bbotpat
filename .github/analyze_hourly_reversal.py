#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Hourly reversal backtest with:
- Same-bar exit filtering (default ON)
- Trading fees (bps, non-levered)
- Grid search over z-thresholds, hold windows, SL/TP (fixed %) and ATR-based dynamic SL/TP
- Binance fetch with automatic fallback to Yahoo Finance (yfinance) if geo-blocked (HTTP 451)

Usage example (GitHub Actions step):
  python .github/analyze_hourly_reversal.py \
    --symbols BTCUSDT,ETHUSDT,SOLUSDT \
    --start 2023-01-01 --end now \
    --top-k 10 --fee-bps 8

Notes:
- No leverage is applied anywhere.
- Fees are applied on entry and exit: 2 * fee_bps per round trip.
"""

import argparse
import math
import sys
import time
from datetime import datetime, timezone
from typing import Dict, List, Tuple, Optional

import numpy as np
import pandas as pd
import requests
from dateutil import parser as dtparser

# Optional import; we only use it if Binance is blocked or fails
try:
    import yfinance as yf
    HAVE_YF = True
except Exception:
    HAVE_YF = False

BINANCE_BASE = "https://api.binance.com"

# ----------------------------
# Data fetching
# ----------------------------

def to_ms(dt: datetime) -> int:
    return int(dt.timestamp() * 1000)

def parse_when(s: str) -> datetime:
    if s.lower() == "now":
        return datetime.now(timezone.utc)
    dt = dtparser.parse(s)
    # make UTC if naive
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    else:
        dt = dt.astimezone(timezone.utc)
    return dt

def fetch_binance_klines(symbol: str, interval: str, start_ms: int, end_ms: int) -> List[List]:
    """
    Fetch klines from Binance in chunks (max 1000 candles per call).
    Returns raw kline rows. Raises on HTTP errors except 451 which we bubble up.
    """
    limit = 1000
    klines: List[List] = []
    cur = start_ms
    # safety loop cap
    for _ in range(20000):
        params = {
            "symbol": symbol,
            "interval": interval,
            "startTime": cur,
            "endTime": end_ms,
            "limit": limit,
        }
        url = f"{BINANCE_BASE}/api/v3/klines"
        r = requests.get(url, params=params, timeout=20)
        if r.status_code == 451:
            # geo-block
            raise RuntimeError(f"BINANCE_451: {r.text}")
        if r.status_code >= 400:
            raise RuntimeError(f"Binance error {r.status_code}: {r.text}")
        batch = r.json()
        if not batch:
            break
        klines.extend(batch)
        # next start is last close time + 1ms
        next_ms = batch[-1][6] + 1
        if next_ms >= end_ms:
            break
        # avoid being throttled
        time.sleep(0.05)
        cur = next_ms
    return klines

def klines_to_df(klines: List[List]) -> pd.DataFrame:
    cols = [
        "open_time","open","high","low","close","volume",
        "close_time","quote_asset_volume","num_trades",
        "taker_buy_base","taker_buy_quote","ignore"
    ]
    df = pd.DataFrame(klines, columns=cols)
    df["open_time"] = pd.to_datetime(df["open_time"], unit="ms", utc=True)
    df["close_time"] = pd.to_datetime(df["close_time"], unit="ms", utc=True)
    for col in ("open","high","low","close","volume"):
        df[col] = pd.to_numeric(df[col], errors="coerce")
    df = df.set_index("close_time").sort_index()
    df = df[["open","high","low","close","volume"]]
    # If Binance delivered 1h, index already per-close time. Good enough.
    return df

YF_SYMBOLS = {
    "BTCUSDT": "BTC-USD",
    "ETHUSDT": "ETH-USD",
    "SOLUSDT": "SOL-USD",
}

def fetch_yf_hourly(symbol: str, start_dt: datetime, end_dt: datetime) -> pd.DataFrame:
    """
    Fetch hourly bars via yfinance as a fallback. Uses -USD pairs.
    """
    if not HAVE_YF:
        raise RuntimeError("yfinance not installed and Binance unavailable.")
    yf_sym = YF_SYMBOLS.get(symbol.upper())
    if yf_sym is None:
        raise RuntimeError(f"No yfinance mapping for symbol {symbol}")
    # yfinance expects naive or local; we pass ISO strings
    start_str = start_dt.strftime("%Y-%m-%d %H:%M:%S")
    end_str = end_dt.strftime("%Y-%m-%d %H:%M:%S")
    df = yf.download(
        yf_sym,
        start=start_str,
        end=end_str,
        interval="60m",
        auto_adjust=False,
        progress=False,
        threads=True,
    )
    if df is None or df.empty:
        raise RuntimeError(f"yfinance returned no data for {yf_sym}")
    # Standardize columns to lower-case
    df = df.rename(columns={
        "Open":"open","High":"high","Low":"low","Close":"close","Volume":"volume"
    })
    # Make index UTC
    if df.index.tz is None:
        df.index = df.index.tz_localize(timezone.utc)
    else:
        df.index = df.index.tz_convert(timezone.utc)
    df = df[["open","high","low","close","volume"]].dropna()
    # yfinance sometimes delivers more granular or missing last bar; resample to strict 1H close
    df = df.resample("1H").agg({
        "open":"first","high":"max","low":"min","close":"last","volume":"sum"
    }).dropna()
    return df

def fetch_klines(symbol: str, interval: str, start_ms: int, end_ms: int) -> pd.DataFrame:
    """
    Try Binance first; if 451 or other fatal error occurs, fall back to yfinance hourly.
    Only '1h' interval is supported end-to-end for this analysis.
    """
    if interval not in ("1h","1H","60m"):
        raise ValueError("Only 1h interval is supported in this script.")
    start_dt = datetime.fromtimestamp(start_ms/1000, tz=timezone.utc)
    end_dt = datetime.fromtimestamp(end_ms/1000, tz=timezone.utc)

    try:
        print(f"Downloading {symbol} 1h from Binance…")
        raw = fetch_binance_klines(symbol, "1h", start_ms, end_ms)
        df = klines_to_df(raw)
        if df.empty:
            raise RuntimeError("Empty Binance dataframe")
        print(f"  Got {len(df)} bars from {df.index[0]} → {df.index[-1]}")
        return df
    except RuntimeError as e:
        msg = str(e)
        print(f"  Binance fetch failed: {msg}")
        print("  Falling back to yfinance hourly…")
        df = fetch_yf_hourly(symbol, start_dt, end_dt)
        print(f"  Got {len(df)} bars from {df.index[0]} → {df.index[-1]}")
        return df

# ----------------------------
# Indicators & backtest logic
# ----------------------------

def rolling_zscore(series: pd.Series, lookback: int) -> pd.Series:
    mean = series.rolling(lookback, min_periods=lookback).mean()
    std = series.rolling(lookback, min_periods=lookback).std(ddof=0)
    z = (series - mean) / std
    return z

def atr(df: pd.DataFrame, period: int = 14) -> pd.Series:
    h, l, c = df["high"], df["low"], df["close"]
    prev_c = c.shift(1)
    tr = pd.concat([
        (h - l),
        (h - prev_c).abs(),
        (l - prev_c).abs()
    ], axis=1).max(axis=1)
    return tr.rolling(period, min_periods=period).mean()

@dataclass
class Trade:
    index: pd.Timestamp
    side: str   # "LONG" or "SHORT"
    entry: float
    exit: float
    exit_index: pd.Timestamp
    pnl: float
    roi: float
    reason: str

from dataclasses import dataclass

def simulate_trades(
    df: pd.DataFrame,
    z: pd.Series,
    z_thresh: float,
    hold_hours: int,
    fee_bps: float = 8.0,
    filter_same_bar_exit: bool = True,
    sl_pct: Optional[float] = 3.0,
    tp_pct: Optional[float] = 2.0,
    use_atr: bool = False,
    atr_mult_sl: float = 1.5,
    atr_mult_tp: float = 1.0,
    atr_period: int = 14,
) -> Tuple[List[Trade], float]:
    """
    Very simple reversal model:
    - LONG when z <= -z_thresh
    - SHORT when z >=  z_thresh
    - Exit by TP/SL or time-based hold (whichever comes first)
    - Fees applied on entry AND exit: fee_bps each way (no leverage)
    - If filter_same_bar_exit=True, require at least next bar for exit (so a signal bar cannot exit on the same bar)
    """
    fees_rt = 2.0 * (fee_bps / 10000.0)  # round trip fee fraction

    series = df.copy()
    series["z"] = z
    series = series.dropna(subset=["z"]).copy()

    # Pre-compute ATR if needed
    series["atr"] = atr(series, atr_period) if use_atr else np.nan

    trades: List[Trade] = []
    equity = 100.0  # start $100 to compare configs easily

    # Iterate through bars; when a signal appears, open a position at CLOSE of that bar
    # Exit rules are checked on subsequent bars
    idx = series.index
    closes = series["close"].values
    highs = series["high"].values
    lows = series["low"].values
    zs = series["z"].values
    atrs = series["atr"].values

    n = len(series)
    i = 0
    while i < n:
        cz = zs[i]
        entry_ts = idx[i]
        entry_px = closes[i]
        opened = False
        side = None
        stop = None
        take = None
        max_i = min(n-1, i + hold_hours)  # last bar index allowed (inclusive) for hold exit

        if cz >= z_thresh:
            side = "SHORT"
            opened = True
        elif cz <= -z_thresh:
            side = "LONG"
            opened = True

        if opened:
            # Set SL/TP either fixed % or ATR-based
            if use_atr and not np.isnan(atrs[i]):
                # Use ATR (price-based). SL = atr_mult_sl * ATR, TP = atr_mult_tp * ATR
                sl_val = atrs[i] * atr_mult_sl
                tp_val = atrs[i] * atr_mult_tp
                if side == "LONG":
                    stop = entry_px - sl_val
                    take = entry_px + tp_val
                else:
                    stop = entry_px + sl_val
                    take = entry_px - tp_val
            else:
                # Fixed % from entry
                if side == "LONG":
                    stop = entry_px * (1.0 - (sl_pct/100.0)) if sl_pct is not None else -math.inf
                    take = entry_px * (1.0 + (tp_pct/100.0)) if tp_pct is not None else math.inf
                else:
                    stop = entry_px * (1.0 + (sl_pct/100.0)) if sl_pct is not None else math.inf
                    take = entry_px * (1.0 - (tp_pct/100.0)) if tp_pct is not None else -math.inf

            # Determine from which bar exits are allowed
            exit_start = i + 1 if filter_same_bar_exit else i

            exit_px = None
            exit_ts = None
            reason = "HOLD_EXPIRED"

            j = exit_start
            while j <= max_i:
                hi = highs[j]
                lo = lows[j]
                cl = closes[j]
                # Check intrabar hit
                hit_tp = False
                hit_sl = False
                if side == "LONG":
                    if hi >= take:
                        exit_px = take
                        reason = "TP"
                        hit_tp = True
                    if lo <= stop:
                        # If both SL and TP could be hit the same bar, we take the worse (SL) conservatively
                        exit_px = stop if not hit_tp else min(stop, take)
                        reason = "SL" if not hit_tp else ("SL" if stop < take else "TP")
                        hit_sl = True
                else:  # SHORT
                    if lo <= take:
                        exit_px = take
                        reason = "TP"
                        hit_tp = True
                    if hi >= stop:
                        exit_px = stop if not hit_tp else max(stop, take)
                        reason = "SL" if not hit_tp else ("SL" if stop > take else "TP")
                        hit_sl = True

                if hit_tp or hit_sl:
                    exit_ts = idx[j]
                    break

                # time exit at close of j if last allowed bar
                if j == max_i:
                    exit_px = cl
                    exit_ts = idx[j]
                    reason = "TIME"
                    break

                j += 1

            if exit_px is None:
                # Should not happen; fallback safety
                exit_px = closes[max_i]
                exit_ts = idx[max_i]
                reason = "TIME_FALLBACK"

            # Compute ROI and apply fees (non-levered)
            if side == "LONG":
                gross = (exit_px / entry_px) - 1.0
            else:
                gross = (entry_px / exit_px) - 1.0

            net = gross - fees_rt
            roi_pct = net * 100.0
            equity *= (1.0 + net)

            trades.append(Trade(
                index=entry_ts, side=side, entry=entry_px,
                exit=exit_px, exit_index=exit_ts, pnl=net, roi=roi_pct,
                reason=reason
            ))

            # Move to bar after exit to avoid overlapping entries
            i = j + 1
            continue

        i += 1

    return trades, equity

def summarize(trades: List[Trade], equity: float) -> Dict[str, float]:
    if not trades:
        return {"trades": 0, "win_rate": 0.0, "final_equity": equity, "avg_roi": 0.0}
    rets = np.array([t.pnl for t in trades], dtype=float)
    wins = (rets > 0).sum()
    wr = 100.0 * wins / len(trades)
    avg = 100.0 * rets.mean()
    return {
        "trades": len(trades),
        "win_rate": wr,
        "final_equity": equity,
        "avg_roi": avg,
    }

def grid_search(
    df: pd.DataFrame,
    symbol: str,
    z_lb: int = 48,
    fee_bps: float = 8.0,
    filter_same_bar_exit: bool = True,
) -> pd.DataFrame:
    """
    Try a small grid of configurations:
      - z_thresh: 1.5, 2.0, 2.5
      - hold_hours: 12, 24, 48
      - Fixed SL/TP (%): (sl,tp) in {(4,2), (4,3), (5,3)}
      - ATR-based: atr_mult_sl in {1.5, 2.0}, atr_mult_tp in {1.0, 1.5}
    """
    close = df["close"]
    z = rolling_zscore(close.pct_change().fillna(0).cumsum(), z_lb)  # z of cumulative returns (slow drift capture)

    rows = []
    configs = []

    z_thresholds = [1.5, 2.0, 2.5]
    holds = [12, 24, 48]

    # Fixed SL/TP grid
    fixed_pairs = [(4.0, 2.0), (4.0, 3.0), (5.0, 3.0)]

    # ATR grid
    atr_sl_mults = [1.5, 2.0]
    atr_tp_mults = [1.0, 1.5]

    for zt in z_thresholds:
        for hh in holds:
            # fixed variants
            for slp, tpp in fixed_pairs:
                trades, eq = simulate_trades(
                    df, z, z_thresh=zt, hold_hours=hh,
                    fee_bps=fee_bps,
                    filter_same_bar_exit=filter_same_bar_exit,
                    sl_pct=slp, tp_pct=tpp,
                    use_atr=False
                )
                summ = summarize(trades, eq)
                rows.append({
                    "symbol": symbol,
                    "z": zt,
                    "hold_h": hh,
                    "mode": "fixed",
                    "sl_pct": slp,
                    "tp_pct": tpp,
                    "final": summ["final_equity"],
                    "trades": summ["trades"],
                    "win_rate": summ["win_rate"],
                    "avg_roi": summ["avg_roi"],
                })
                configs.append((trades, (zt, hh, "fixed", slp, tpp)))

            # atr variants
            for msl in atr_sl_mults:
                for mtp in atr_tp_mults:
                    trades, eq = simulate_trades(
                        df, z, z_thresh=zt, hold_hours=hh,
                        fee_bps=fee_bps,
                        filter_same_bar_exit=filter_same_bar_exit,
                        sl_pct=None, tp_pct=None,
                        use_atr=True, atr_mult_sl=msl, atr_mult_tp=mtp
                    )
                    summ = summarize(trades, eq)
                    rows.append({
                        "symbol": symbol,
                        "z": zt,
                        "hold_h": hh,
                        "mode": "atr",
                        "atr_sl": msl,
                        "atr_tp": mtp,
                        "final": summ["final_equity"],
                        "trades": summ["trades"],
                        "win_rate": summ["win_rate"],
                        "avg_roi": summ["avg_roi"],
                    })
                    configs.append((trades, (zt, hh, "atr", msl, mtp)))

    out = pd.DataFrame(rows).sort_values("final", ascending=False).reset_index(drop=True)
    out._configs = configs  # stash trades + params for later inspection
    return out

# ----------------------------
# CLI / Main
# ----------------------------

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbols", type=str, default="BTCUSDT,ETHUSDT,SOLUSDT",
                    help="Comma-separated symbols (Binance format). Fallback to yfinance BTC-USD/ETH-USD/SOL-USD.")
    ap.add_argument("--start", type=str, default="2023-01-01")
    ap.add_argument("--end", type=str, default="now")
    ap.add_argument("--interval", type=str, default="1h")
    ap.add_argument("--top-k", type=int, default=10)
    ap.add_argument("--fee-bps", type=float, default=8.0, help="Per-side fee in basis points (non-levered).")
    ap.add_argument("--z-lookback", type=int, default=48, help="Z-score lookback (hours).")
    ap.add_argument("--no-samebar-filter", action="store_true", help="Allow same bar exits (default blocks them).")
    args = ap.parse_args()

    syms = [s.strip().upper() for s in args.symbols.split(",") if s.strip()]
    start_dt = parse_when(args.start)
    end_dt = parse_when(args.end)
    start_ms = to_ms(start_dt)
    end_ms = to_ms(end_dt)

    print("Fetching hourly data…")
    all_results = []

    for s in syms:
        print(f"Downloading {s} 1h…")
        df = fetch_klines(s, args.interval, start_ms, end_ms)
        if len(df) < 2000:
            print(f"  Warning: only {len(df)} bars. Results may be noisy.")

        res = grid_search(
            df, s, z_lb=args.z_lookback,
            fee_bps=args.fee_bps,
            filter_same_bar_exit=(not args.no_samebar_filter)
        )
        all_results.append(res)

        # Show top-K for this symbol
        topk = res.head(args.top_k).copy()
        print(f"\n=== Top {args.top_k} configurations for {s} (fees={args.fee_bps} bps/side, same-bar exits={'OFF' if not args.no_samebar_filter else 'ON'}) ===")
        for i, row in topk.iterrows():
            if row["mode"] == "fixed":
                desc = f"z≥{row['z']} hold={int(row['hold_h'])}h FIXED sl={row['sl_pct']}% tp={row['tp_pct']}%"
            else:
                desc = f"z≥{row['z']} hold={int(row['hold_h'])}h ATR sl={row['atr_sl']}× tp={row['atr_tp']}×"
            print(f"{i+1:2d}. {desc} → {row['final']:.2f} ({(row['final']-100):.2f}%)  trades={int(row['trades'])}  win={row['win_rate']:.1f}%  avgROI={row['avg_roi']:.2f}%")

        # Also dump the detailed trades for the single best config
        best_trades, best_params = res._configs[0]
        print(f"\n=== Best Config Detailed Trades for {s} ===")
        bp = best_params
        if bp[2] == "fixed":
            print(f"  Config: z≥{bp[0]} hold={bp[1]}h FIXED SL={bp[3]}% TP={bp[4]}%  (fees={args.fee_bps} bps/side)")
        else:
            print(f"  Config: z≥{bp[0]} hold={bp[1]}h ATR SL={bp[3]}× TP={bp[4]}×  (fees={args.fee_bps} bps/side)")
        equity = 100.0
        for k, t in enumerate(best_trades[:200], start=1):  # cap print to first 200 for logs brevity
            equity *= (1.0 + t.pnl)
            print(f"{k:3d} {s:8s} {t.side:5s} {t.index} → {t.exit_index}  ROI={t.roi:+.2f}%  After=${equity:.2f}  [{t.reason}]")
        if len(best_trades) > 200:
            print(f"... ({len(best_trades)-200} more trades suppressed in log)")

    # Combine tables & print quick leaderboard across all symbols
    big = pd.concat(all_results, ignore_index=True)
    big = big.sort_values("final", ascending=False).reset_index(drop=True)
    print(f"\n=== Global Top {args.top_k} Configs Across Symbols ===")
    for i, row in big.head(args.top_k).iterrows():
        if row["mode"] == "fixed":
            desc = f"{row['symbol']} z≥{row['z']} hold={int(row['hold_h'])}h FIXED sl={row['sl_pct']}% tp={row['tp_pct']}%"
        else:
            desc = f"{row['symbol']} z≥{row['z']} hold={int(row['hold_h'])}h ATR sl={row['atr_sl']}× tp={row['atr_tp']}×"
        print(f"{i+1:2d}. {desc} → {row['final']:.2f} ({(row['final']-100):.2f}%)  trades={int(row['trades'])}  win={row['win_rate']:.1f}%  avgROI={row['avg_roi']:.2f}%")

if __name__ == "__main__":
    main()
