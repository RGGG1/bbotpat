#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Extreme Outlier Hourly Reversal Backtest (No Leverage, Fee-Aware, No Same-Bar Exits)
with:
  - Time-of-day gating (UTC hours whitelist)
  - Dynamic TP/SL scaling by |Z|
  - Regime filter via rolling volatility percentile
  - Cross-symbol confirmation (e.g., suppress SOL long if BTC is overbought)

Data:
  - CCXT via OKX by default (region-friendly), purely hourly OHLCV.
  - No Binance / Yahoo fallback to avoid rate/region issues.

Install (locally or in CI):
  pip install --upgrade pip
  pip install pandas numpy ccxt python-dateutil

Usage example:
  python .github/analyze_extreme_outliers.py \
      --symbols BTCUSDT ETHUSDT SOLUSDT \
      --start 2023-01-01 --end now \
      --fee-bps 8 --exchange okx --side both \
      --utc-hours 0,1,2,12,13,14,15,16,17,18,23 \
      --vol-pctl-min 40 --vol-lookback 336 \
      --dynamic-z-ref 3.5 --dynamic-scale-min 0.8 --dynamic-scale-max 1.6 \
      --confirm-map SOLUSDT:BTCUSDT --confirm-z 2.5 --confirm-mode opposite \
      --print-top 7
"""

from __future__ import annotations

import argparse
import json
import math
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import List, Dict, Tuple, Optional, Set

import numpy as np
import pandas as pd

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
    if len(series) == 0:
        return series
    out = series.copy()
    first = min(period, len(series))
    out.iloc[:first] = series.iloc[:first].mean()
    for i in range(first, len(series)):
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


def realized_vol(close: pd.Series, lookback: int) -> pd.Series:
    """Rolling annualized-ish RV proxy over 'lookback' hours (use simple std)."""
    ret1 = np.log(close / close.shift(1))
    rv = ret1.rolling(lookback, min_periods=lookback).std(ddof=1) * np.sqrt(24 * 365)
    return rv


def to_pctl(series: pd.Series, lookback: int) -> pd.Series:
    """Rolling percentile rank (0-100)."""
    def pct_rank(x):
        last = x.iloc[-1]
        rank = (x <= last).mean()
        return rank * 100.0
    return series.rolling(lookback, min_periods=lookback).apply(pct_rank, raw=False)


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
    Paginate hourly OHLCV via CCXT from 'since' (ms) up to 'until' (ms), inclusive when aligned.
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
# Strategy: Extreme Outlier Reversal (XOR) + refinements
# ---------------------------

@dataclass
class Params:
    ret_lookback: int
    ret_horizons: Tuple[int, ...]
    ret_z: float
    vol_lookback: int
    vol_z: float
    bb_period: int
    bb_k: float
    atr_period: int
    sl_atr: float
    tp_atr: float
    cooldown_bars: int
    min_hold_bars: int
    side: str
    # NEW
    utc_hours: Optional[Set[int]]                 # None = no gating
    vol_pctl_min: Optional[float]                 # regime filter minimum percentile (0-100) or None
    vol_pctl_lookback: Optional[int]
    dynamic_z_ref: Optional[float]                # reference |Z| for scaling
    dynamic_scale_min: float                      # clamp lower bound for scale
    dynamic_scale_max: float                      # clamp upper bound for scale
    confirm_symbol: Optional[str]                 # symbol name for cross confirmation (e.g., BTCUSDT)
    confirm_z: Optional[float]                    # z threshold on confirm symbol
    confirm_mode: Optional[str]                   # 'opposite' or 'same' (see logic below)


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
    out["atr"] = compute_atr(out, p.atr_period)

    # Returns and horizons
    out["ret1"] = np.log(out["close"] / out["close"].shift(1))
    for h in p.ret_horizons:
        out[f"ret_{h}h"] = out["ret1"].rolling(h, min_periods=h).sum()

    # Z-scores
    for h in p.ret_horizons:
        r = out[f"ret_{h}h"]
        out[f"z_{h}h"] = rolling_zscore(r, p.ret_lookback)

    # Volume Z-score
    out["vol_z"] = rolling_zscore(out["volume"], p.vol_lookback)

    # Bollinger
    lower, upper = bollinger_bands(out["close"], p.bb_period, p.bb_k)
    out["bb_lower"] = lower
    out["bb_upper"] = upper

    # Realized vol & percentile for regime filtering
    if p.vol_pctl_lookback:
        rv = realized_vol(out["close"], p.vol_pctl_lookback)
        out["rv"] = rv
        out["rv_pctl"] = to_pctl(rv, p.vol_pctl_lookback)
    else:
        out["rv_pctl"] = np.nan

    return out


def signal_row(row: pd.Series, horizons: Tuple[int, ...], z_thr: float) -> int:
    """
    Return +1 (long), -1 (short), or 0 (none) based on extreme z-scores and band breach.
    Require any horizon to exceed |z_thr|.
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
    return 0


def dynamic_scale_from_z(abs_z: float, z_ref: float, smin: float, smax: float) -> float:
    """Scale proportional to |z| / z_ref, clamped."""
    if z_ref is None or z_ref <= 0:
        return 1.0
    raw = abs_z / z_ref
    return max(smin, min(smax, raw))


def compute_bar_z_extreme(row: pd.Series, horizons: Tuple[int, ...]) -> float:
    """Return the *most extreme* |z| across horizons for this bar (nan-safe)."""
    zs = [row.get(f"z_{h}h", np.nan) for h in horizons]
    zs = [z for z in zs if np.isfinite(z)]
    if not zs:
        return float("nan")
    return float(max(abs(z) for z in zs))


def backtest(df: pd.DataFrame,
             p: Params,
             fee_bps_side: float,
             confirm_df: Optional[pd.DataFrame] = None) -> Result:
    """
    Discrete 1h bars, next-open entry, ATR-based SL/TP, no same-bar exit.
    Fee model: subtract (fee_bps_side * 2) bps per round-trip on notional.
    Enhancements: time gating, regime filter, dynamic scaling, cross-symbol confirmation.
    """
    data = prepare_features(df, p)

    # If cross-confirmation is used, prepare its features minimally (only z-values and bands)
    if confirm_df is not None:
        c = confirm_df.copy()
        # match index via outer join later; compute 1/3/6h returns using the same lookbacks/horizons
        c["ret1"] = np.log(c["close"] / c["close"].shift(1))
        for h in p.ret_horizons:
            c[f"ret_{h}h"] = c["ret1"].rolling(h, min_periods=h).sum()
            c[f"z_{h}h"] = rolling_zscore(c[f"ret_{h}h"], p.ret_lookback)
        # We don't require its Bollinger/vol_z for confirmation
        confirm_df = c

    # Build primary signals with all gating
    sig = []
    for ts, row in data.iterrows():
        # Time-of-day gating
        if p.utc_hours is not None and ts.hour not in p.utc_hours:
            sig.append(0); continue

        # Regime filter
        if p.vol_pctl_min is not None and np.isfinite(row.get("rv_pctl", np.nan)):
            if row["rv_pctl"] < p.vol_pctl_min:
                sig.append(0); continue

        # Require ATR + bands ready
        if np.isnan(row["atr"]) or np.isnan(row["bb_lower"]) or np.isnan(row["bb_upper"]):
            sig.append(0); continue

        # Require volume attention
        if not (row.get("vol_z", 0) >= p.vol_z):
            sig.append(0); continue

        s = signal_row(row, p.ret_horizons, p.ret_z)

        # Side restriction
        if p.side == "long" and s == -1: s = 0
        if p.side == "short" and s == +1: s = 0

        # Cross-symbol confirmation (optional)
        if s != 0 and confirm_df is not None and p.confirm_z is not None and p.confirm_mode in ("opposite", "same"):
            # Get confirm bar; align on timestamp
            if ts in confirm_df.index:
                crow = confirm_df.loc[ts]
                # Max |z| and sign on confirm symbol
                czs = [crow.get(f"z_{h}h", np.nan) for h in p.ret_horizons]
                czs = [z for z in czs if np.isfinite(z)]
                if czs:
                    cmax = max(czs)
                    cmin = min(czs)
                    cabs = max(abs(c) for c in czs)
                    # Modes:
                    #  - 'opposite': for LONG on target, require confirm symbol NOT strongly overbought
                    #                (i.e., max z < confirm_z). For SHORT, require NOT strongly oversold
                    #                (i.e., min z > -confirm_z). This avoids fading when the leader is also extreme in the same direction.
                    #  - 'same':     require confirm symbol to be extreme in the *same* direction
                    if p.confirm_mode == "opposite":
                        if s > 0 and cmax >= p.confirm_z:  # confirm is very positive -> skip long fade
                            s = 0
                        if s < 0 and cmin <= -p.confirm_z: # confirm is very negative -> skip short fade
                            s = 0
                    elif p.confirm_mode == "same":
                        if s > 0 and cmin > -p.confirm_z:
                            s = 0
                        if s < 0 and cmax < p.confirm_z:
                            s = 0
                else:
                    s = 0  # no confirm data for this ts -> skip
            else:
                s = 0  # not aligned -> skip

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

    while i < n - 1:
        ts = idx[i]
        if cooldown_until is not None and ts < cooldown_until:
            i += 1
            continue

        s = data["sig"].iloc[i]
        if s == 0:
            i += 1
            continue

        # Entry next bar open
        entry_i = i + 1
        if entry_i >= n:
            break
        entry = float(data["open"].iloc[entry_i])
        atr = float(data["atr"].iloc[i])
        if not math.isfinite(entry) or not math.isfinite(atr) or atr <= 0:
            i += 1
            continue

        # Dynamic scaling for SL/TP by |Z|
        scale = 1.0
        if p.dynamic_z_ref is not None and p.dynamic_z_ref > 0:
            bar_abs_z = compute_bar_z_extreme(data.iloc[i], p.ret_horizons)
            if math.isfinite(bar_abs_z):
                scale = dynamic_scale_from_z(bar_abs_z, p.dynamic_z_ref,
                                             p.dynamic_scale_min, p.dynamic_scale_max)

        slx = p.sl_atr * scale
        tpx = p.tp_atr * scale

        # Levels
        if s > 0:
            stop = entry - slx * atr
            take = entry + tpx * atr
        else:
            stop = entry + slx * atr
            take = entry - tpx * atr

        # Walk forward; no same-bar exits: start from entry_i + min_hold_bars
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
                # conservative tie-breaker: adverse first
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

        if exit_price is None:
            exit_price = float(data["close"].iloc[-1])
            exit_i = n - 1

        gross_r = (exit_price / entry - 1.0) if s > 0 else (1.0 - exit_price / entry)
        net_r = gross_r - fee_rt
        rets_pct.append(net_r * 100.0)
        equity *= (1.0 + net_r)

        n_trades += 1
        if net_r > 0:
            wins += 1

        cooldown_until = idx[min(exit_i + p.cooldown_bars, n - 1)]
        i = exit_i + 1

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

def search_params(df: pd.DataFrame,
                  base_side: str,
                  fee_bps: float,
                  confirm_df: Optional[pd.DataFrame],
                  base_params: Dict) -> Tuple[Result, List[Result]]:
    """
    Compact grid over reasonable ranges to find robust regions.
    Includes gating/regime/dynamic/confirm from base_params.
    """
    # Core grid
    ret_lookbacks = [720, 1440]          # 30d, 60d (1h bars)
    ret_horizon_sets = [(1, 3, 6)]
    z_thrs = [3.0, 3.5, 4.0]
    vol_lookbacks = [720, 1440]
    vol_zs = [1.0, 1.5]
    bb_periods = [100, 200]
    bb_ks = [1.5, 2.0]
    atr_periods = [14, 21]
    sl_multiples = [1.5, 2.0, 2.5]
    tp_multiples = [2.0, 2.5, 3.0]
    cooldowns = [4, 8]
    min_hold = [1]
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
                                                            # Carry-through refinements
                                                            utc_hours=base_params.get("utc_hours"),
                                                            vol_pctl_min=base_params.get("vol_pctl_min"),
                                                            vol_pctl_lookback=base_params.get("vol_pctl_lookback"),
                                                            dynamic_z_ref=base_params.get("dynamic_z_ref"),
                                                            dynamic_scale_min=base_params.get("dynamic_scale_min"),
                                                            dynamic_scale_max=base_params.get("dynamic_scale_max"),
                                                            confirm_symbol=base_params.get("confirm_symbol"),
                                                            confirm_z=base_params.get("confirm_z"),
                                                            confirm_mode=base_params.get("confirm_mode"),
                                                        )
                                                        r = backtest(df, p, fee_bps_side=fee_bps, confirm_df=confirm_df)
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
            "MH": pr.min_hold_bars,
            "UTC_Hrs": ",".join(map(str, sorted(pr.utc_hours))) if pr.utc_hours else "all",
            "VolPctlMin": pr.vol_pctl_min if pr.vol_pctl_min is not None else "none",
            "DynZref": pr.dynamic_z_ref if pr.dynamic_z_ref is not None else "none",
            "DynMin": pr.dynamic_scale_min,
            "DynMax": pr.dynamic_scale_max,
            "Confirm": pr.confirm_symbol if pr.confirm_symbol else "none",
            "CMode": pr.confirm_mode if pr.confirm_mode else "none",
            "Cz": pr.confirm_z if pr.confirm_z is not None else "none",
        })
    return pd.DataFrame(rows)


# ---------------------------
# CLI
# ---------------------------

def parse_confirm_map(s: Optional[str]) -> Dict[str, str]:
    """
    Parse "A:B,C:D" into {"A": "B", "C": "D"}.
    """
    out = {}
    if not s:
        return out
    parts = [p.strip() for p in s.split(",") if p.strip()]
    for p in parts:
        if ":" in p:
            k, v = p.split(":", 1)
            out[k.strip()] = v.strip()
    return out


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

    # Refinements
    ap.add_argument("--utc-hours", type=str, default="",
                    help="Comma-separated UTC hours to trade (e.g., '13,14,15,16'). Empty = all.")
    ap.add_argument("--vol-pctl-min", type=float, default=None,
                    help="Regime: minimum realized-vol percentile (0-100). None=off.")
    ap.add_argument("--vol-lookback", type=int, default=336,
                    help="Lookback (bars) for realized vol & percentile (default 336 = 2 weeks).")
    ap.add_argument("--dynamic-z-ref", type=float, default=None,
                    help="Dynamic scaling reference |Z|. None=off.")
    ap.add_argument("--dynamic-scale-min", type=float, default=0.8,
                    help="Clamp min scale for dynamic SL/TP (default 0.8).")
    ap.add_argument("--dynamic-scale-max", type=float, default=1.6,
                    help="Clamp max scale for dynamic SL/TP (default 1.6).")

    # Cross-symbol confirmation
    ap.add_argument("--confirm-map", type=str, default="",
                    help="Mapping like 'SOLUSDT:BTCUSDT,ETHUSDT:BTCUSDT'. If a symbol appears as key, it'll use the mapped symbol to confirm.")
    ap.add_argument("--confirm-z", type=float, default=2.5,
                    help="Z threshold on confirm symbol.")
    ap.add_argument("--confirm-mode", type=str, default="opposite", choices=["opposite", "same"],
                    help="Confirmation mode: 'opposite' (avoid fading when leader is same-direction extreme) or 'same' (require same-direction extreme).")

    ap.add_argument("--print-top", type=int, default=5, help="How many top configs to print.")
    args = ap.parse_args()

    start_dt = parse_date(args.start)
    end_dt = parse_date(args.end)
    end_dt = end_dt.replace(minute=0, second=0, microsecond=0)
    if end_dt <= start_dt:
        raise SystemExit("End must be after start.")

    # Parse utc-hours
    utc_hours_set: Optional[Set[int]] = None
    if args.utc_hours.strip():
        try:
            hrs = sorted({int(h.strip()) for h in args.utc_hours.split(",") if h.strip() != ""})
            for h in hrs:
                if h < 0 or h > 23:
                    raise ValueError
            utc_hours_set = set(hrs)
        except Exception:
            raise SystemExit("--utc-hours must be comma-separated integers in [0..23]")

    # Confirm map
    confirm_map = parse_confirm_map(args.confirm-map if hasattr(args, 'confirm-map') else args.confirm_map)

    print("Running extreme outlier backtest with refinements...")
    print(f"Range: {start_dt.isoformat()} → {end_dt.isoformat()}  (UTC)")
    print(f"Exchange: {args.exchange}  Fee: {args.fee_bps} bps/side  Side: {args.side}")
    if utc_hours_set is None:
        print("UTC hours: all")
    else:
        print(f"UTC hours: {sorted(utc_hours_set)}")
    print(f"Regime filter: vol_pctl_min={args.vol_pctl_min} lookback={args.vol_lookback}")
    print(f"Dynamic: z_ref={args.dynamic_z_ref} scale[{args.dynamic_scale_min},{args.dynamic_scale_max}]")
    if confirm_map:
        print(f"Cross-confirm map: {confirm_map}   mode={args.confirm_mode}  z={args.confirm_z}")
    else:
        print("Cross-confirm: off")
    print("No leverage. No same-bar exits. ATR SL/TP. CCXT only.\n")

    summary_rows = []

    # Preload any confirm symbols needed (dedupe list)
    needed_conf_syms = sorted(set(confirm_map.values()))
    confirm_data: Dict[str, pd.DataFrame] = {}
    for cs in needed_conf_syms:
        try:
            cdf = get_hourly_df(cs, start_dt, end_dt, exchange_id=args.exchange)
            confirm_data[cs] = cdf
            print(f"  [confirm] loaded {cs}: {len(cdf):,} bars {cdf.index[0]} → {cdf.index[-1]}")
        except Exception as e:
            print(f"  [confirm] FAILED {cs}: {e}")

    for sym in args.symbols:
        print(f"\n=== {sym} ===")
        try:
            df = get_hourly_df(sym, start_dt, end_dt, exchange_id=args.exchange)
        except Exception as e:
            print(f"  Data fetch failed for {sym}: {e}")
            continue

        print(f"  got {len(df):,} bars {df.index[0]} → {df.index[-1]}")

        base_params = {
            "utc_hours": utc_hours_set,
            "vol_pctl_min": args.vol_pctl_min,
            "vol_pctl_lookback": args.vol_lookback,
            "dynamic_z_ref": args.dynamic_z_ref,
            "dynamic_scale_min": args.dynamic_scale_min,
            "dynamic_scale_max": args.dynamic_scale_max,
            "confirm_symbol": confirm_map.get(sym, None),
            "confirm_z": args.confirm_z if confirm_map.get(sym, None) else None,
            "confirm_mode": args.confirm_mode if confirm_map.get(sym, None) else None,
        }

        cdf = None
        if base_params["confirm_symbol"]:
            cdf = confirm_data.get(base_params["confirm_symbol"])
            if cdf is None:
                print(f"  (warning) Missing confirm data for {base_params['confirm_symbol']}; confirmation disabled for this symbol.")
                base_params["confirm_symbol"] = None
                base_params["confirm_z"] = None
                base_params["confirm_mode"] = None

        best, all_results = search_params(df, base_side=args.side, fee_bps=args.fee_bps,
                                          confirm_df=cdf, base_params=base_params)

        print("  Best configuration:")
        print(f"    Final equity: ${best.final_equity:.2f}  Trades: {best.n_trades}  Win%: {best.win_rate:.1f}%")
        print(f"    AvgR: {best.avg_r_pct:.3f}%  MedR: {best.med_r_pct:.3f}%")
        bp = best.params
        print(f"    Side={bp.side}  Z≥{bp.ret_z}  retLB={bp.ret_lookback}  volLB={bp.vol_lookback}  "
              f"BB[{bp.bb_period},{bp.bb_k}]  ATR={bp.atr_period}  SLx={bp.sl_atr}  TPx={bp.tp_atr}  "
              f"CD={bp.cooldown_bars}  MH={bp.min_hold_bars}  "
              f"UTC={sorted(bp.utc_hours) if bp.utc_hours else 'all'}  "
              f"RegimeMin={bp.vol_pctl_min if bp.vol_pctl_min is not None else 'none'}  "
              f"DynZref={bp.dynamic_z_ref if bp.dynamic_z_ref is not None else 'none'}  "
              f"Dyn[{bp.dynamic_scale_min},{bp.dynamic_scale_max}]  "
              f"Confirm={bp.confirm_symbol if bp.confirm_symbol else 'none'}:{bp.confirm_mode if bp.confirm_mode else 'none'}@{bp.confirm_z if bp.confirm_z is not None else 'none'}")

        top_df = summarize_top(all_results, k=args.print_top)
        if not top_df.empty:
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
                "TPx": bp.tp_atr,
                "UTC": ",".join(map(str, sorted(bp.utc_hours))) if bp.utc_hours else "all",
                "RegimeMin": bp.vol_pctl_min if bp.vol_pctl_min is not None else "none",
                "DynZref": bp.dynamic_z_ref if bp.dynamic_z_ref is not None else "none",
                "Confirm": bp.confirm_symbol if bp.confirm_symbol else "none",
                "CMode": bp.confirm_mode if bp.confirm_mode else "none",
                "Cz": bp.confirm_z if bp.confirm_z is not None else "none",
            })
        else:
            print("  (No top results to display)")

    if summary_rows:
        print("\n=== Summary ===")
        sdf = pd.DataFrame(summary_rows)
        cols = ["Symbol","BestEq$","Trades","Win%","AvgR%","MedR%","Side","Z","SLx","TPx",
                "UTC","RegimeMin","DynZref","Confirm","CMode","Cz"]
        sdf = sdf[cols]
        print(sdf.to_string(index=False))
    else:
        print("\nNo symbols succeeded.")


if __name__ == "__main__":
    main()
