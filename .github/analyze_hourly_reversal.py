# .github/analyze_hourly_reversal.py
# Hourly reversal backtest with z-score entries, fixed % TP/SL and ATR-based TP/SL,
# fees in bps (UNLEVERED), min-hold (no same-bar exits), and best-config reporting.
# Leverage is DISABLED and forced to 1x regardless of flags.

import argparse
import math
import os
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from typing import List, Dict, Optional, Tuple

import numpy as np
import pandas as pd
import requests
from dateutil import parser as dtparser

BINANCE_BASE = "https://api.binance.com"

# -----------------------------
# Utilities
# -----------------------------
def ensure_dir(path: str):
    os.makedirs(path, exist_ok=True)

def parse_when(s: str) -> int:
    """Return ms since epoch (UTC). Accepts 'now' or ISO8601."""
    if s.lower() == "now":
        return int(datetime.now(timezone.utc).timestamp() * 1000)
    dt = dtparser.parse(s)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    else:
        dt = dt.astimezone(timezone.utc)
    return int(dt.timestamp() * 1000)

# -----------------------------
# Data Fetch
# -----------------------------
def fetch_klines(symbol: str, interval: str, start_ms: int, end_ms: int) -> pd.DataFrame:
    """
    Robustly fetch klines from Binance with pagination.
    """
    url = f"{BINANCE_BASE}/api/v3/klines}"
    # fix accidental brace if copy-pasted
    url = f"{BINANCE_BASE}/api/v3/klines"
    limit = 1000
    rows = []
    curr = start_ms
    last_open = None

    while True:
        params = {"symbol": symbol, "interval": interval, "limit": limit, "startTime": curr, "endTime": end_ms}
        r = requests.get(url, params=params, timeout=30)
        if r.status_code != 200:
            raise RuntimeError(f"Binance error {r.status_code}: {r.text}")
        data = r.json()
        if not data:
            break

        if last_open is not None and data[0][0] == last_open and len(data) == 1:
            break
        last_open = data[-1][0]

        rows.extend(data)
        next_ms = data[-1][0] + 1
        if next_ms >= end_ms:
            break
        curr = next_ms

    if not rows:
        raise RuntimeError("No klines returned.")

    cols = [
        "open_time","open","high","low","close","volume",
        "close_time","qav","num_trades","taker_base","taker_quote","ignore"
    ]
    df = pd.DataFrame(rows, columns=cols)
    for c in ("open","high","low","close","volume"):
        df[c] = df[c].astype(float)
    df["open_time"] = pd.to_datetime(df["open_time"], unit="ms", utc=True)
    df["close_time"] = pd.to_datetime(df["close_time"], unit="ms", utc=True)
    df = df.sort_values("close_time").reset_index(drop=True).set_index("close_time")
    return df[["open","high","low","close","volume","open_time"]]

# -----------------------------
# Indicators
# -----------------------------
def add_returns_zscore(df: pd.DataFrame, ret_lookback: int) -> pd.DataFrame:
    out = df.copy()
    out["ret"] = np.log(out["close"]).diff()
    out["ret_mean"] = out["ret"].rolling(ret_lookback, min_periods=ret_lookback).mean()
    out["ret_std"] = out["ret"].rolling(ret_lookback, min_periods=ret_lookback).std(ddof=0)
    out["z"] = (out["ret"] - out["ret_mean"]) / out["ret_std"]
    return out

def add_atr(df: pd.DataFrame, atr_len: int = 14) -> pd.DataFrame:
    out = df.copy()
    prev_close = out["close"].shift(1)
    tr1 = out["high"] - out["low"]
    tr2 = (out["high"] - prev_close).abs()
    tr3 = (out["low"] - prev_close).abs()
    tr = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)
    out["atr"] = tr.rolling(atr_len, min_periods=atr_len).mean()
    return out

# -----------------------------
# Backtest Core
# -----------------------------
@dataclass
class Trade:
    i_entry: int
    i_exit: int
    t_entry: pd.Timestamp
    t_exit: pd.Timestamp
    side: str         # "LONG" or "SHORT"
    entry: float
    exit: float
    roi: float        # net PnL as fraction of equity at entry (after fees)
    equity_after: float
    reason: str       # "TP", "SL", "TIME", etc.

@dataclass
class Config:
    symbol: str
    scheme: str       # "pct" or "atr"
    z_th: float
    min_hold: int
    max_hold: int
    tp: float         # if scheme="pct": TP% as decimal; if "atr": k_atr multiplier
    sl: float         # if scheme="pct": SL% as decimal; if "atr": m_atr multiplier
    ret_lookback: int
    atr_len: int
    fee_bps: float    # per side, UNLEVERED

def round_trip_roi_unlevered(entry: float, exit: float, side: str, fee_bps: float) -> float:
    """
    Return net ROI fraction on equity (UNLEVERED) after fees per side.
    Fees charged entry + exit, each = fee_bps / 1e4 of notional.
    """
    px_chg = (exit / entry - 1.0)
    direction = +1.0 if side == "LONG" else -1.0
    gross = direction * px_chg
    fee_per_side = (fee_bps / 1e4)
    net = gross - 2.0 * fee_per_side
    return net

def simulate_config(df: pd.DataFrame, cfg: Config) -> Tuple[List[Trade], float]:
    """
    Enter when z <= -z_th (LONG) or z >= +z_th (SHORT).
    Exits: TP / SL intrabar (after min_hold), or time exit at max_hold bars.
    If TP and SL touch same bar, assume SL first (conservative).
    """
    equity = 100.0
    trades: List[Trade] = []

    z = df["z"].values
    close = df["close"].values
    high = df["high"].values
    low = df["low"].values
    atr = df["atr"].values
    times = df.index.to_list()

    in_pos = False
    side = None
    i_entry = None
    entry_px = None

    n = len(df)

    def levels(entry_px_local: float, bar_i: int) -> Tuple[float,float]:
        if cfg.scheme == "pct":
            if side == "LONG":
                tp_px = entry_px_local * (1.0 + cfg.tp)
                sl_px = entry_px_local * (1.0 - cfg.sl)
            else:
                tp_px = entry_px_local * (1.0 - cfg.tp)
                sl_px = entry_px_local * (1.0 + cfg.sl)
            return tp_px, sl_px
        else:
            a = atr[bar_i]
            if np.isnan(a) or a <= 0:
                return (math.inf, -math.inf) if side == "LONG" else (-math.inf, math.inf)
            dist_tp = cfg.tp * a
            dist_sl = cfg.sl * a
            if side == "LONG":
                tp_px = entry_px_local + dist_tp
                sl_px = entry_px_local - dist_sl
            else:
                tp_px = entry_px_local - dist_tp
                sl_px = entry_px_local + dist_sl
            return tp_px, sl_px

    i = 0
    while i < n:
        if not in_pos:
            if i == 0 or np.isnan(z[i]):
                i += 1; continue

            if z[i] <= -cfg.z_th:
                in_pos = True; side = "LONG"; i_entry = i; entry_px = close[i]
                i += 1; continue
            elif z[i] >= cfg.z_th:
                in_pos = True; side = "SHORT"; i_entry = i; entry_px = close[i]
                i += 1; continue
            else:
                i += 1; continue
        else:
            bars_held = i - i_entry
            can_exit = bars_held >= cfg.min_hold

            do_exit = False
            reason = "TIME"
            exit_px = close[i]

            if can_exit:
                tp_px, sl_px = levels(entry_px, i)

                if side == "LONG":
                    hit_tp = high[i] >= tp_px
                    hit_sl = low[i] <= sl_px
                    if hit_tp and hit_sl:
                        do_exit = True; exit_px = sl_px; reason = "SL"
                    elif hit_sl:
                        do_exit = True; exit_px = sl_px; reason = "SL"
                    elif hit_tp:
                        do_exit = True; exit_px = tp_px; reason = "TP"
                else:
                    hit_tp = low[i] <= tp_px
                    hit_sl = high[i] >= sl_px
                    if hit_tp and hit_sl:
                        do_exit = True; exit_px = sl_px; reason = "SL"
                    elif hit_sl:
                        do_exit = True; exit_px = sl_px; reason = "SL"
                    elif hit_tp:
                        do_exit = True; exit_px = tp_px; reason = "TP"

            if not do_exit and bars_held >= cfg.max_hold:
                do_exit = True; reason = "TIME"; exit_px = close[i]

            if do_exit:
                net = round_trip_roi_unlevered(entry_px, exit_px, side, cfg.fee_bps)
                equity *= (1.0 + net)
                trades.append(Trade(
                    i_entry=i_entry, i_exit=i, t_entry=times[i_entry], t_exit=times[i],
                    side=side, entry=entry_px, exit=exit_px, roi=net, equity_after=equity, reason=reason
                ))
                in_pos = False; side = None; i_entry = None; entry_px = None
                i += 1
            else:
                i += 1

    return trades, equity

# -----------------------------
# Sweeps / Reporting
# -----------------------------
def sweep_configs(
    df_by_symbol: Dict[str, pd.DataFrame],
    symbols: List[str],
    fee_bps: float,
    min_hold: int,
    ret_lookback: int,
    max_hold_grid: List[int],
    z_grid: List[float],
    pct_tp_sl_grid: List[Tuple[float,float]],
    atr_tp_sl_grid: List[Tuple[float,float]],
    atr_len: int,
    top_k: int = 10
) -> Tuple[pd.DataFrame, Dict[str, List[Trade]], Dict[str, Config]]:
    rows = []
    best_trades_by_symbol: Dict[str, List[Trade]] = {}
    best_cfg_by_symbol: Dict[str, Config] = {}

    for sym in symbols:
        df = df_by_symbol[sym]
        cfgs: List[Config] = []

        for z_th in z_grid:
            for max_hold in max_hold_grid:
                for tp, sl in pct_tp_sl_grid:
                    cfgs.append(Config(
                        symbol=sym, scheme="pct", z_th=z_th, min_hold=min_hold, max_hold=max_hold,
                        tp=tp, sl=sl, ret_lookback=ret_lookback, atr_len=atr_len, fee_bps=fee_bps
                    ))
                for tpA, slA in atr_tp_sl_grid:
                    cfgs.append(Config(
                        symbol=sym, scheme="atr", z_th=z_th, min_hold=min_hold, max_hold=max_hold,
                        tp=tpA, sl=slA, ret_lookback=ret_lookback, atr_len=atr_len, fee_bps=fee_bps
                    ))

        best_equity = -1.0
        best_cfg = None
        best_trades = None

        for cfg in cfgs:
            trades, eq = simulate_config(df, cfg)
            ret = {
                "symbol": cfg.symbol,
                "scheme": cfg.scheme,
                "z": cfg.z_th,
                "min_hold_bars": cfg.min_hold,
                "max_hold_bars": cfg.max_hold,
                "tp" if cfg.scheme=="pct" else "tp_atr": cfg.tp,
                "sl" if cfg.scheme=="pct" else "sl_atr": cfg.sl,
                "ret_lookback": cfg.ret_lookback,
                "atr_len": cfg.atr_len,
                "fee_bps": cfg.fee_bps,
                "trades": len(trades),
                "final_equity": eq,
                "roi_pct": (eq/100.0 - 1.0)*100.0
            }
            rows.append(ret)

            if eq > best_equity:
                best_equity = eq
                best_cfg = cfg
                best_trades = trades

        best_trades_by_symbol[sym] = best_trades or []
        if best_cfg:
            best_cfg_by_symbol[sym] = best_cfg

    summary = pd.DataFrame(rows).sort_values("final_equity", ascending=False).reset_index(drop=True)
    summary_top = summary.head(top_k) if top_k > 0 else summary
    return summary_top, best_trades_by_symbol, best_cfg_by_symbol

def print_top(summary: pd.DataFrame, title: str, k: int = 10):
    print(f"\n=== {title} ===")
    for i, row in summary.head(k).iterrows():
        sym = row["symbol"]
        scheme = row["scheme"]
        z = row["z"]
        min_hold = int(row["min_hold_bars"])
        max_hold = int(row["max_hold_bars"])
        trades = int(row["trades"])
        fe = row["final_equity"]
        roi = row["roi_pct"]
        if scheme == "pct":
            print(f"{i+1:2d}. {sym} {scheme} z≥{z:.1f} hold=[{min_hold},{max_hold}] "
                  f"TP={row['tp']*100:.1f}% SL={row['sl']*100:.1f}% → {fe:,.2f} ({roi:,.2f}%)  trades={trades}")
        else:
            print(f"{i+1:2d}. {sym} {scheme} z≥{z:.1f} hold=[{min_hold},{max_hold}] "
                  f"TP={row['tp_atr']}×ATR SL={row['sl_atr']}×ATR → {fe:,.2f} ({roi:,.2f}%)  trades={trades}")

def save_outputs(summary: pd.DataFrame,
                 best_trades_by_symbol: Dict[str, List[Trade]],
                 best_cfg_by_symbol: Dict[str, Config],
                 outdir: str):
    ensure_dir(outdir)
    summary_path = os.path.join(outdir, "hourly_reversal_summary.csv")
    summary.to_csv(summary_path, index=False)

    for sym, trades in best_trades_by_symbol.items():
        if sym not in best_cfg_by_symbol: 
            continue
        cfg = best_cfg_by_symbol[sym]
        rows = []
        for n, tr in enumerate(trades, 1):
            rows.append({
                "n": n,
                "symbol": sym,
                "side": tr.side,
                "t_entry": tr.t_entry.isoformat(),
                "t_exit": tr.t_exit.isoformat(),
                "entry": tr.entry,
                "exit": tr.exit,
                "roi_frac": tr.roi,
                "roi_pct": tr.roi*100.0,
                "equity_after": tr.equity_after,
                "reason": tr.reason
            })
        df_tr = pd.DataFrame(rows)
        trades_path = os.path.join(outdir, f"best_trades_{sym}.csv")
        df_tr.to_csv(trades_path, index=False)

        cfg_path = os.path.join(outdir, f"best_config_{sym}.json")
        with open(cfg_path, "w") as f:
            import json
            json.dump(asdict(cfg), f, indent=2)

# -----------------------------
# Main
# -----------------------------
def main():
    ap = argparse.ArgumentParser(description="Hourly Reversal Backtest (fees UNLEVERED, min-hold, ATR variants)")
    ap.add_argument("--symbols", type=str, default="BTCUSDT,ETHUSDT,SOLUSDT",
                    help="Comma-separated symbols (Binance).")
    ap.add_argument("--interval", type=str, default="1h")
    ap.add_argument("--start", type=str, default="2023-01-01")
    ap.add_argument("--end", type=str, default="now")
    ap.add_argument("--ret-lookback", type=int, default=24, help="Bars for rolling z-score (on 1h returns).")
    ap.add_argument("--atr-len", type=int, default=14, help="ATR length (for ATR-based exits).")
    ap.add_argument("--z-grid", type=str, default="1.5,2.0,2.5", help="z thresholds to test (e.g. '1.5,2.0,2.5').")
    ap.add_argument("--max-hold-grid", type=str, default="12,24,48", help="Max hold (bars) to test.")
    ap.add_argument("--pct-tpsl", type=str, default="0.02x0.03,0.03x0.045,0.04x0.06",
                    help="Comma list of TPxSL in % as decimals, e.g. '0.02x0.03,0.03x0.045'")
    ap.add_argument("--atr-tpsl", type=str, default="1.5x2.0,2.0x3.0,3.0x4.5",
                    help="Comma list of TPxSL in ATR multiples, e.g. '2.0x3.0,3.0x4.5'")
    ap.add_argument("--min-hold-bars", type=int, default=1, help="Minimum hold bars (prevents same-bar exits).")
    # Retained for CLI compatibility, but ignored:
    ap.add_argument("--leverage", type=float, default=1.0, help="IGNORED. Leverage is disabled and forced to 1x.")
    ap.add_argument("--fee-bps", type=float, default=6.0, help="Fee per side in basis points (UNLEVERED).")
    ap.add_argument("--top-k", type=int, default=10)
    ap.add_argument("--outdir", type=str, default=".github/output")
    args = ap.parse_args()

    if abs(args.leverage - 1.0) > 1e-9:
        print("NOTE: --leverage is ignored. Leverage is disabled and forced to 1x (fees are unlevered).")

    start_ms = parse_when(args.start)
    end_ms = parse_when(args.end)

    syms = [s.strip().upper() for s in args.symbols.split(",") if s.strip()]
    print("Fetching hourly data…")
    df_by_symbol: Dict[str, pd.DataFrame] = {}
    for s in syms:
        print(f"Downloading {s} {args.interval}…")
        ohlc = fetch_klines(s, args.interval, start_ms, end_ms)
        print(f"  Got {len(ohlc)} bars from {ohlc.index[0]} → {ohlc.index[-1]}")
        if len(ohlc) < 2000:
            raise RuntimeError(f"Only {len(ohlc)} hourly bars returned; expected many more.")
        ohlc = add_returns_zscore(ohlc, args.ret_lookback)
        ohlc = add_atr(ohlc, args.atr_len)
        df_by_symbol[s] = ohlc

    z_grid = [float(x) for x in args.z_grid.split(",")]
    max_hold_grid = [int(x) for x in args.max_hold_grid.split(",")]

    def parse_pairs(spec: str) -> List[Tuple[float, float]]:
        out = []
        for part in spec.split(","):
            part = part.strip()
            if not part:
                continue
            if "x" not in part:
                raise ValueError(f"Bad TP/SL pair: {part}")
            a, b = part.split("x")
            out.append((float(a), float(b)))
        return out

    pct_pairs = parse_pairs(args.pct_tpsl)
    atr_pairs = parse_pairs(args.atr_tpsl)

    summary, best_trades_by_symbol, best_cfg_by_symbol = sweep_configs(
        df_by_symbol=df_by_symbol,
        symbols=syms,
        fee_bps=args.fee_bps,
        min_hold=args.min_hold_bars,
        ret_lookback=args.ret_lookback,
        max_hold_grid=max_hold_grid,
        z_grid=z_grid,
        pct_tp_sl_grid=pct_pairs,
        atr_tp_sl_grid=atr_pairs,
        atr_len=args.atr_len,
        top_k=args.top_k
    )

    print_top(summary, f"Top {args.top_k} configurations (fees={args.fee_bps}bps/side, min-hold={args.min_hold_bars}, lev=1×)", k=args.top_k)

    for sym in syms:
        cfg = best_cfg_by_symbol.get(sym)
        if not cfg:
            continue
        print(f"\n=== Best Config Detailed ( {sym} ) ===")
        if cfg.scheme == "pct":
            extra = f"TP={cfg.tp*100:.1f}% SL={cfg.sl*100:.1f}%"
        else:
            extra = f"TP={cfg.tp}×ATR SL={cfg.sl}×ATR"
        print(f"{sym} {cfg.scheme} z≥{cfg.z_th} hold=[{cfg.min_hold},{cfg.max_hold}] {extra}")
        trades = best_trades_by_symbol.get(sym, [])[:20]
        for n, t in enumerate(trades, 1):
            print(f"{n:2d} {sym:7s} {t.side:5s} {t.t_entry} → {t.t_exit} ROI={t.roi*100:+.2f}%  After=${t.equity_after:,.2f} {t.reason}")

    save_outputs(summary, best_trades_by_symbol, best_cfg_by_symbol, args.outdir)
    print(f"\nSaved summary and best-trade logs to: {args.outdir}")

if __name__ == "__main__":
    main()
