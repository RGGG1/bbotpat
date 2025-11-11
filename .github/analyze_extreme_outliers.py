#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Extreme Outlier Hourly Reversal Backtest (No Leverage, Fee-Aware, No Same-Bar Exits)

Key ideas:
- Identify rare extremes: big negative/positive return Z-scores over multiple horizons,
  volume Z-score spike, and Bollinger band breach.
- Enter on the NEXT bar open (no same-bar exits allowed by construction).
- Manage risk with ATR-based SL/TP; grid-search a *small, tractable* set of parameters.
- Fee in bps/side (round-trip = 2 * fee_bps). No leverage applied anywhere.
- Uses CCXT to fetch hourly OHLCV from OKX by default (region-friendly; deep history).

Usage examples:
  python .github/analyze_extreme_outliers.py \
      --symbols BTCUSDT ETHUSDT SOLUSDT \
      --start 2023-01-01 --end now \
      --fee-bps 8 --side both

  python .github/analyze_extreme_outliers.py --help
"""

from __future__ import annotations

import argparse
import math
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import List, Dict, Tuple, Optional

import numpy as np
import pandas as pd

# CCXT for exchange data
try:
    import ccxt
except Exception as e:
    raise SystemExit("ccxt is required. Install with: pip install ccxt") from e

UTC = timezone.utc


# ---------------------------
# Utilities
# ---------------------------

def parse_date(s: str) -> datetime:
    """Parse YYYY-MM-DD or 'now' (UTC)."""
    if s.lower() == "now":
        return datetime.now(tz=UTC)
    return datetime.strptime(s, "%Y-%m-%d").replace(tzinfo=UTC)


def symbol_to_ccxt(sym: str) -> str:
    """Convert common 'BTCUSDT' to CCXT 'BTC/USDT' if needed."""
    if "/" in sym:
        return sym
    if sym.endswith("USDT"):
        return sym[:-4] + "/USDT"
    if sym.endswith("USD"):
        return sym[:-3] + "/USD"
    # Fallback: try to insert slash before last 3-4 chars
    if len(sym) > 4:
        return sym[:-4] + "/" + sym[-4:]
    return sym


def rolling_zscore(series: pd.Series, window: int) -> pd.Series:
    """Z-score with rolling mean/std (unbiased)."""
    mean = series.rolling(window, min_periods=window).mean()
    std = series.rolling(window, min_periods=window).std(ddof=1)
    z = (series - mean) / std
    return z.replace([np.inf, -np.inf], np.nan)


def true_range(high: pd.Series, low: pd.Series, close: pd.Series) -> pd.Series:
    prev_close = close.shift(1)
    tr = pd.concat([
        (high - low).abs(),
        (high - prev_close).abs(),
        (low - prev_close).abs()
    ], axis=1).max(axis=1)
    return tr


def wilder_smooth(series: pd.Series, period: int) -> pd.Series:
    """Wilder's smoothing (EMA-like) for ATR."""
    out = series.copy()
    out.iloc[:period] = series.iloc[:period].mean()
    for i in range(period, len(series)):
        out.iloc[i] = (out.iloc[i - 1] * (period - 1) + series.iloc[i]) / period
    return out


def compute_atr(df: pd.DataFrame, period: int) -> pd.Series:
    tr = true_range(df["high"], df["low"], df["close"])
    return wilder_smooth(tr, period)


def bollinger_bands(close: pd.Series, period: int, k: float) -> Tuple[pd.Series, pd.Series]:
    ma = close.rolling(period, min_periods=period).mean()
    sd = close.rolling(period, min_periods=period).std(ddof=1)
    upper = ma + k * sd
    lower = ma - k * sd
    return lower, upper


# ---------------------------
# Data Fetch (CCXT / OKX)
# ---------------------------

def fetch_ohlcv_ccxt(
    exchange_id: str,
    symbol: str,
    timeframe: str,
    since: int,
    until: int,
    limit: int = 1000
) -> List[List[float]]:
    """
    Paginate hourly OHLCV via CCXT from 'since' (ms) up to 'until' (ms), inclusive of both ends when aligned.
    """
    ex = getattr(ccxt, exchange_id)()
    ex.load_markets()
    out: List[List[float]] = []
    tf_ms = ex.parse_timeframe(timeframe) * 1000
    t = since

    while True:
        batch = ex.fetch_ohlcv(symbol, timeframe=timeframe, since=t, limit=limit)
        if not batch:
            break
        # Filter to 'until'
        batch = [row for row in batch if row[0] <= until]
        if not batch:
            break
        out.extend(batch)
        t_next = batch[-1][0] + tf_ms
        if t_next > until:
            break
        # guard against no progress
        if t_next <= t:
            break
        t = t_next

    return out


def get_hourly_df(symbol_raw: str, start_dt: datetime, end_dt: datetime,
                  exchange_id: str = "okx") -> pd.DataFrame:
    sym = symbol_to_ccxt(symbol_raw)
    since = int(start_dt.timestamp() * 1000)
    until = int(end_dt.timestamp() * 1000)
    data = fetch_ohlcv_ccxt(exchange_id, sym, "1h", since, until, limit=1000)
    if not data:
        raise RuntimeError(f"No OHLCV returned for {symbol_raw} via {exchange_id}.")

    df = pd.DataFrame(data, columns=["ts", "open", "high", "low", "close", "volume"])
    df["ts"] = pd.to_datetime(df["ts"], unit="ms", utc=True)
    df = df.drop_duplicates("ts").set_index("ts").sort_index()
    return df


# ---------------------------
# Strategy: Extreme Outlier Reversal (XOR)
# ---------------------------

@dataclass
class Params:
    ret_lookback: int      # rolling window for Z-score of returns (in bars)
    ret_horizons: Tuple[int, ...]  # horizons in bars, e.g., (1,3,6)
    ret_z: float           # min |z| threshold to consider an extreme
    vol_lookback: int      # rolling window for volume z-score
    vol_z: float           # min volume zscore to require (liquidity / attention)
    bb_period: int         # period for Bollinger bands
    bb_k: float            # k for Bollinger bands
    atr_period: int        # ATR period for risk
    sl_atr: float          # stop in ATR multiples
    tp_atr: float          # take-profit in ATR multiples
    cooldown_bars: int     # skip N bars after any exit
    min_hold_bars: int     # disallow exits on the entry bar (>=1)
    side: str              # 'long' | 'short' | 'both'


@dataclass
class Result:
    final_equity: float
    n_trades: int
    win_rate: float
    avg_r_pct: float
    med_r_pct: float
    params: Params


def prepare_features(df: pd.DataFrame, p: Params) -> pd.DataFrame:
    out = df.copy()
    # ATR for risk
    out["atr"] = compute_atr(out, p.atr_period)

    # Returns (log) and multi-horizon cumulative returns
    out["ret1"] = np.log(out["close"] / out["close"].shift(1))
    for h in p.ret_horizons:
        out[f"ret_{h}h"] = out["ret1"].rolling(h, min_periods=h).sum()

    # Z-score per horizon using same lookback
    for h in p.ret_horizons:
        r = out[f"ret_{h}h"]
        out[f"z_{h}h"] = rolling_zscore(r, p.ret_lookback)

    # Volume Z-score
    out["vol_z"] = rolling_zscore(out["volume"], p.vol_lookback)

    # Bollinger
    lower, upper = bollinger_bands(out["close"], p.bb_period, p.bb_k)
    out["bb_lower"] = lower
    out["bb_upper"] = upper

    return out


def signal_row(row: pd.Series, horizons: Tuple[int, ...], z_thr: float) -> int:
    """
    Return +1 (long), -1 (short), or 0 (none) based on extreme z-scores and band breach.
    We require *any* horizon to exceed |z_thr|.
    """
    z_values = [row.get(f"z_{h}h", np.nan) for h in horizons]
    if np.any(np.isnan(z_values)):
        return 0
    z_min = np.nanmin(z_values)
    z_max = np.nanmax(z_values)

    long_ok = (z_min <= -z_thr) and (row["close"] <= row.get("bb_lower", np.nan))
    short_ok = (z_max >= z_thr) and (row["close"] >= row.get("bb_upper", np.nan))

    if long_ok and not short_ok:
        return +1
    if short_ok and not long_ok:
        return -1
    # If both true (extremely rare), abstain to avoid ambiguity
    return 0


def backtest(df: pd.DataFrame, p: Params, fee_bps_side: float) -> Result:
    """
    Discrete 1h bars, next-open entry, ATR-based SL/TP, no same-bar exit.
    Fee model: subtract (fee_bps_side * 2) bps per round-trip on notional.
    """
    data = df.copy()
    data = prepare_features(data, p)

    # Build signals where volume spike is present
    sig = []
    for ts, row in data.iterrows():
        if np.isnan(row["atr"]) or np.isnan(row["bb_lower"]) or np.isnan(row["bb_upper"]):
            sig.append(0)
            continue
        # require volume attention
        if not (row.get("vol_z", 0) >= p.vol_z):
            sig.append(0)
            continue
        s = signal_row(row, p.ret_horizons, p.ret_z)
        if p.side == "long" and s == -1:
            s = 0
        elif p.side == "short" and s == +1:
            s = 0
        sig.append(s)
    data["sig"] = sig

    equity = 100.0
    n_trades = 0
    wins = 0
    rets_pct: List[float] = []
    fee_rt = (fee_bps_side * 2.0) / 1e4  # round-trip as fraction

    i = 0
    idx = data.index
    n = len(data)

    cooldown_until: Optional[pd.Timestamp] = None

    while i < n - 1:  # need at least one bar ahead to enter
        ts = idx[i]
        if cooldown_until is not None and ts < cooldown_until:
            i += 1
            continue

        s = data["sig"].iloc[i]
        if s == 0:
            i += 1
            continue

        # Entry on next bar open
        entry_i = i + 1
        if entry_i >= n:
            break
        entry = float(data["open"].iloc[entry_i])
        atr = float(data["atr"].iloc[i])
        if not math.isfinite(entry) or not math.isfinite(atr) or atr <= 0:
            i += 1
            continue

        # SL/TP levels
        if s > 0:
            stop = entry - p.sl_atr * atr
            take = entry + p.tp_atr * atr
        else:
            stop = entry + p.sl_atr * atr
            take = entry - p.tp_atr * atr

        # Walk forward bar-by-bar, no same-bar exit: start from entry_i + min_hold_bars
        j = max(entry_i + p.min_hold_bars, entry_i + 1)
        exit_price = None
        exit_i = None
        while j < n:
            hi = float(data["high"].iloc[j])
            lo = float(data["low"].iloc[j])

            if s > 0:
                hit_tp = hi >= take
                hit_sl = lo <= stop
            else:
                hit_tp = lo <= take
                hit_sl = hi >= stop

            if hit_tp and hit_sl:
                # Ambiguous within the hour; use conservative tie-breaker (assume adverse fills first)
                # This prevents optimism and is realistic on 1h bars.
                exit_price = stop
                exit_i = j
                break
            elif hit_tp:
                exit_price = take
                exit_i = j
                break
            elif hit_sl:
                exit_price = stop
                exit_i = j
                break

            j += 1

        # If never hit SL/TP by end, exit on last close
        if exit_price is None:
            exit_price = float(data["close"].iloc[-1])
            exit_i = n - 1

        # PnL (no leverage, fee applied once per round trip)
        gross_r = (exit_price / entry - 1.0) if s > 0 else (1.0 - exit_price / entry)
        net_r = gross_r - fee_rt
        rets_pct.append(net_r * 100.0)
        equity *= (1.0 + net_r)

        n_trades += 1
        if net_r > 0:
            wins += 1

        # Cooldown
        cooldown_until = idx[min(exit_i + p.cooldown_bars, n - 1)]
        i = exit_i + 1  # continue after exit

    win_rate = (wins / n_trades * 100.0) if n_trades > 0 else 0.0
    avg_r = float(np.mean(rets_pct)) if rets_pct else 0.0
    med_r = float(np.median(rets_pct)) if rets_pct else 0.0

    return Result(
        final_equity=equity,
        n_trades=n_trades,
        win_rate=win_rate,
        avg_r_pct=avg_r,
        med_r_pct=med_r,
        params=p
    )


# ---------------------------
# Parameter Search
# ---------------------------

def search_params(df: pd.DataFrame, base_side: str, fee_bps: float) -> Tuple[Result, List[Result]]:
    """
    Compact grid over reasonable ranges to find robust regions.
    """
    # Keep the grid small enough for CI while covering useful combinations
    ret_lookbacks = [720, 1440]          # 30d, 60d (on 1h bars)
    ret_horizon_sets = [(1, 3, 6)]
    z_thrs = [3.0, 3.5, 4.0]
    vol_lookbacks = [720, 1440]
    vol_zs = [1.0, 1.5]
    bb_periods = [100, 200]
    bb_ks = [1.5, 2.0]
    atr_periods = [14, 21]
    sl_multiples = [1.5, 2.0, 2.5]
    tp_multiples = [2.0, 2.5, 3.0]
    cooldowns = [4, 8]       # 4–8 hours
    min_hold = [1]           # strictly disallow same-bar exits
    sides = [base_side] if base_side in ("long", "short") else ["both"]

    all_results: List[Result] = []
    best: Optional[Result] = None

    for side in sides:
        for rl in ret_lookbacks:
            for rhs in ret_horizon_sets:
                for zt in z_thrs:
                    for vlb in vol_lookbacks:
                        for vz in vol_zs:
                            for bbp in bb_periods:
                                for bbk in bb_ks:
                                    for ap in atr_periods:
                                        for slx in sl_multiples:
                                            for tpx in tp_multiples:
                                                for cd in cooldowns:
                                                    for mh in min_hold:
                                                        p = Params(
                                                            ret_lookback=rl,
                                                            ret_horizons=rhs,
                                                            ret_z=zt,
                                                            vol_lookback=vlb,
                                                            vol_z=vz,
                                                            bb_period=bbp,
                                                            bb_k=bbk,
                                                            atr_period=ap,
                                                            sl_atr=slx,
                                                            tp_atr=tpx,
                                                            cooldown_bars=cd,
                                                            min_hold_bars=mh,
                                                            side=side if side != "both" else "both",
                                                        )
                                                        r = backtest(df, p, fee_bps_side=fee_bps)
                                                        all_results.append(r)
                                                        if (best is None) or (r.final_equity > best.final_equity):
                                                            best = r
    assert best is not None
    return best, all_results


def summarize_top(results: List[Result], k: int = 5) -> pd.DataFrame:
    res = sorted(results, key=lambda r: r.final_equity, reverse=True)[:k]
    rows = []
    for r in res:
        pr = r.params
        rows.append({
            "FinalEq": round(r.final_equity, 2),
            "Trades": r.n_trades,
            "Win%": round(r.win_rate, 1),
            "AvgR%": round(r.avg_r_pct, 3),
            "MedR%": round(r.med_r_pct, 3),
            "Side": pr.side,
            "Z": pr.ret_z,
            "retLB": pr.ret_lookback,
            "volLB": pr.vol_lookback,
            "BBp": pr.bb_period,
            "BBk": pr.bb_k,
            "ATR": pr.atr_period,
            "SLx": pr.sl_atr,
            "TPx": pr.tp_atr,
            "CD": pr.cooldown_bars,
            "MH": pr.min_hold_bars
        })
    return pd.DataFrame(rows)


# ---------------------------
# CLI
# ---------------------------

def main():
    ap = argparse.ArgumentParser(description="Extreme Outlier Hourly Reversal Backtest (no leverage, fee-aware).")
    ap.add_argument("--symbols", nargs="+", default=["BTCUSDT", "ETHUSDT", "SOLUSDT"],
                    help="Symbols like BTCUSDT or CCXT format BTC/USDT.")
    ap.add_argument("--start", type=str, default="2023-01-01", help="Start date YYYY-MM-DD.")
    ap.add_argument("--end", type=str, default="now", help="End date YYYY-MM-DD or 'now'.")
    ap.add_argument("--fee-bps", type=float, default=8.0, help="Fee per side in bps (no leverage).")
    ap.add_argument("--exchange", type=str, default="okx", help="CCXT exchange id (default okx).")
    ap.add_argument("--side", type=str, default="both", choices=["long", "short", "both"],
                    help="Trade direction.")
    ap.add_argument("--print-top", type=int, default=5, help="How many top configs to print.")
    args = ap.parse_args()

    start_dt = parse_date(args.start)
    end_dt = parse_date(args.end)

    # Align end exclusive (so 'now' won't request a future incomplete candle)
    end_dt = end_dt.replace(minute=0, second=0, microsecond=0)
    if end_dt <= start_dt:
        raise SystemExit("End must be after start.")

    print("Running extreme outlier backtest...")
    print(f"Range: {start_dt.isoformat()} → {end_dt.isoformat()}  (UTC)")
    print(f"Exchange: {args.exchange}  Fee: {args.fee_bps} bps/side  Side: {args.side}")
    print("No leverage. No same-bar exits. ATR SL/TP. CCXT only.\n")

    summary_rows = []

    for sym in args.symbols:
        print(f"=== {sym} ===")
        try:
            df = get_hourly_df(sym, start_dt, end_dt, exchange_id=args.exchange)
        except Exception as e:
            print(f"  Data fetch failed for {sym}: {e}")
            continue

        print(f"  got {len(df):,} bars {df.index[0]} → {df.index[-1]}")

        best, all_results = search_params(df, base_side=args.side, fee_bps=args.fee_bps)

        print("  Best configuration:")
        print(f"    Final equity: ${best.final_equity:.2f}  Trades: {best.n_trades}  Win%: {best.win_rate:.1f}%")
        print(f"    AvgR: {best.avg_r_pct:.3f}%  MedR: {best.med_r_pct:.3f}%")
        bp = best.params
        print(f"    Side={bp.side}  Z≥{bp.ret_z}  retLB={bp.ret_lookback}  volLB={bp.vol_lookback}  "
              f"BB[{bp.bb_period},{bp.bb_k}]  ATR={bp.atr_period}  SLx={bp.sl_atr}  TPx={bp.tp_atr}  "
              f"CD={bp.cooldown_bars}  MH={bp.min_hold_bars}")

        top_df = summarize_top(all_results, k=args.print_top)
        if not top_df.empty:
            # pretty print small table
            print(top_df.to_string(index=False))

            # Store for summary
            summary_rows.append({
                "Symbol": sym,
                "BestEq$": round(best.final_equity, 2),
                "Trades": best.n_trades,
                "Win%": round(best.win_rate, 1),
                "AvgR%": round(best.avg_r_pct, 3),
                "MedR%": round(best.med_r_pct, 3),
                "Side": bp.side,
                "Z": bp.ret_z,
                "SLx": bp.sl_atr,
                "TPx": bp.tp_atr
            })
        else:
            print("  (No top results to display)")

        print()

    if summary_rows:
        print("=== Summary ===")
        sdf = pd.DataFrame(summary_rows)
        # Nice columns order
        sdf = sdf[["Symbol", "BestEq$", "Trades", "Win%", "AvgR%", "MedR%", "Side", "Z", "SLx", "TPx"]]
        print(sdf.to_string(index=False))
    else:
        print("No symbols succeeded.")

if __name__ == "__main__":
    main()
