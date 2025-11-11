#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Hourly Reversal Backtest (No Leverage, Fee-Aware, No Same-Bar Exits)
Resilient data fetch: Binance -> ccxt -> yfinance (chunked to avoid 730d limit)

Examples:
  python .github/analyze_hourly_reversal.py \
    --symbols BTCUSDT,ETHUSDT,SOLUSDT \
    --start 2023-01-01 --end now \
    --fee-bps 8 --min-move-bps 50 \
    --sl-mults 0.75,1.00,1.50,2.00 \
    --tp-mults 0.75,1.25,1.50,2.00

Force a provider (optional):
  --provider binance    # only Binance
  --provider ccxt       # only ccxt (OKX/Bybit/Kraken/KuCoin/Bitfinex/BinanceUS/Coinbase)
  --provider yf         # only yfinance (with chunking)
  --provider auto       # try all (default)
"""

import sys, time, math, json, argparse, os, re
from datetime import datetime, timedelta, timezone
from dataclasses import dataclass
from typing import List, Dict, Tuple, Optional

import numpy as np
import pandas as pd
import requests
from tabulate import tabulate

UTC = timezone.utc

# -------- Utilities: time & parsing --------
def to_ms(dt: datetime) -> int:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return int(dt.timestamp() * 1000)

def from_ms(ms: int) -> datetime:
    return datetime.fromtimestamp(ms / 1000, tz=UTC)

def parse_date_like(s: str, anchor: Optional[datetime] = None) -> datetime:
    """Parse 'YYYY-MM-DD', 'now', 'today', 'yesterday', or rel like '-30d', '-18m', '-2y'."""
    s = str(s).strip().lower()
    now = datetime.now(tz=UTC) if anchor is None else anchor
    if s in ("now",):
        return now
    if s in ("today",):
        return now.replace(hour=0, minute=0, second=0, microsecond=0)
    if s in ("yesterday",):
        t = now - timedelta(days=1)
        return t.replace(hour=0, minute=0, second=0, microsecond=0)
    # relative: -Nd / -Nm / -Ny
    m = re.fullmatch(r"-\s*(\d+)\s*([dmy])", s)
    if m:
        n = int(m.group(1))
        unit = m.group(2)
        if unit == "d":
            return now - timedelta(days=n)
        elif unit == "m":
            # approx months as 30 days to keep deps light
            return now - timedelta(days=30 * n)
        elif unit == "y":
            return now - timedelta(days=365 * n)
    # absolute date
    try:
        return datetime.strptime(s, "%Y-%m-%d").replace(tzinfo=UTC)
    except ValueError as e:
        raise ValueError(f"Could not parse date '{s}'. Use YYYY-MM-DD, now, today, yesterday, or -Nd/-Nm/-Ny.") from e

# -------- Optional deps --------
try:
    import ccxt
    HAVE_CCXT = True
except Exception:
    HAVE_CCXT = False

try:
    import yfinance as yf
    HAVE_YF = True
except Exception:
    HAVE_YF = False

# -------- Data fetching --------
BINANCE_BASE = "https://api.binance.com"

def klines_to_df(klines: List[List]) -> pd.DataFrame:
    cols = ["date", "open", "high", "low", "close", "volume"]
    arr = []
    for k in klines:
        arr.append([from_ms(int(k[0])), float(k[1]), float(k[2]), float(k[3]), float(k[4]), float(k[5])])
    df = pd.DataFrame(arr, columns=cols).sort_values("date").reset_index(drop=True)
    return df

def fetch_binance(symbol: str, interval: str, start_ms: int, end_ms: int, max_retries: int = 5) -> List[List]:
    url = f"{BINANCE_BASE}/api/v3/klines"
    params = {"symbol": symbol, "interval": interval, "startTime": start_ms, "endTime": end_ms, "limit": 1000}
    headers = {"User-Agent": "Mozilla/5.0 (backtest/ci)"}
    out = []
    cursor = start_ms
    while True:
        params["startTime"] = cursor
        retries = 0
        while True:
            r = requests.get(url, params=params, headers=headers, timeout=30)
            if r.status_code == 451:
                raise RuntimeError("BINANCE_451")
            if r.status_code == 429:
                # rate limited: exponential backoff
                sleep_s = min(2 ** retries, 20)
                time.sleep(sleep_s)
                retries += 1
                if retries > max_retries:
                    raise RuntimeError("Binance 429 rate limit (max retries)")
                continue
            if r.status_code != 200:
                raise RuntimeError(f"Binance error {r.status_code}: {r.text}")
            break
        batch = r.json()
        if not batch:
            break
        out.extend(batch)
        next_ms = int(batch[-1][6])  # closeTime
        cursor = next_ms + 1
        if cursor >= end_ms:
            break
        if len(batch) < 1000:
            break
        time.sleep(0.05)
    return out

_EX_SYMBOLS = {
    "BTCUSDT": {
        "okx":       "BTC/USDT",
        "bybit":     "BTC/USDT",
        "kraken":    "XBT/USDT",
        "kucoin":    "BTC/USDT",
        "bitfinex2": "BTC/USDT",
        "binanceus": "BTC/USDT",
        "coinbase":  "BTC/USD",
    },
    "ETHUSDT": {
        "okx":       "ETH/USDT",
        "bybit":     "ETH/USDT",
        "kraken":    "ETH/USDT",
        "kucoin":    "ETH/USDT",
        "bitfinex2": "ETH/USDT",
        "binanceus": "ETH/USDT",
        "coinbase":  "ETH/USD",
    },
    "SOLUSDT": {
        "okx":       "SOL/USDT",
        "bybit":     "SOL/USDT",
        "kraken":    "SOL/USDT",
        "kucoin":    "SOL/USDT",
        "bitfinex2": "SOL/USDT",
        "binanceus": "SOL/USDT",
        "coinbase":  "SOL/USD",
    },
}

_EX_ORDER = ["okx", "bybit", "kraken", "kucoin", "bitfinex2", "binanceus", "coinbase"]

def _ccxt_inst(ex_id: str):
    klass = getattr(ccxt, ex_id)
    ex = klass({"enableRateLimit": True, "timeout": 30000})
    ex.load_markets()
    return ex

def fetch_ccxt_any(symbol_key: str, start_dt: datetime, end_dt: datetime) -> Tuple[pd.DataFrame, Optional[str]]:
    if not HAVE_CCXT:
        return pd.DataFrame(), None
    for ex_id in _EX_ORDER:
        market = _EX_SYMBOLS.get(symbol_key, {}).get(ex_id)
        if not market:
            continue
        try:
            ex = _ccxt_inst(ex_id)
            # small mapping safeguards
            if market not in ex.markets and market.replace("XBT", "BTC") in ex.markets:
                market = market.replace("XBT", "BTC")
            if market not in ex.markets and market.replace("USDT", "USD") in ex.markets:
                market = market.replace("USDT", "USD")

            since = to_ms(start_dt)
            end_ms = to_ms(end_dt)
            all_rows = []
            limit = 1000
            while since < end_ms:
                ohlcv = ex.fetch_ohlcv(market, timeframe="1h", since=since, limit=limit)
                if not ohlcv:
                    break
                all_rows.extend(ohlcv)
                last_ts = ohlcv[-1][0]
                next_ts = last_ts + 60 * 60 * 1000
                if next_ts <= since:
                    break
                since = next_ts
                time.sleep(ex.rateLimit / 1000.0 if getattr(ex, "rateLimit", 0) else 0.25)
            if not all_rows:
                continue

            df = pd.DataFrame(all_rows, columns=["ms","open","high","low","close","volume"])
            df["date"] = df["ms"].apply(lambda x: from_ms(int(x)))
            df = df[["date","open","high","low","close","volume"]].sort_values("date").reset_index(drop=True)
            df = df[df["date"] < end_dt].reset_index(drop=True)
            df = df.drop_duplicates(subset=["date"])
            return df, ex_id
        except Exception:
            continue
    return pd.DataFrame(), None

def _yf_symbol(symbol_key: str) -> Optional[str]:
    return {"BTCUSDT":"BTC-USD","ETHUSDT":"ETH-USD","SOLUSDT":"SOL-USD"}.get(symbol_key)

def fetch_yf_chunked(symbol_key: str, start_dt: datetime, end_dt: datetime) -> pd.DataFrame:
    if not HAVE_YF:
        return pd.DataFrame()
    sym = _yf_symbol(symbol_key)
    if not sym:
        return pd.DataFrame()
    # Yahoo 1h limit ~730 days; pull in <=720-day chunks
    max_span_days = 720
    out_chunks = []
    cur_start = start_dt
    while cur_start < end_dt:
        cur_end = min(cur_start + timedelta(days=max_span_days), end_dt)
        df = yf.download(sym, start=cur_start, end=cur_end, interval="60m", progress=False)
        if df is None or df.empty:
            cur_start = cur_end  # advance to avoid infinite loop
            continue
        df = df.rename(columns={"Open":"open","High":"high","Low":"low","Close":"close","Volume":"volume"})
        if df.index.tz is None:
            df.index = df.index.tz_localize(UTC)
        else:
            df.index = df.index.tz_convert(UTC)
        chunk = df.reset_index().rename(columns={"index":"date","Datetime":"date"})
        chunk = chunk[["date","open","high","low","close","volume"]]
        out_chunks.append(chunk)
        # advance 1 hour after last index to avoid overlap
        if not chunk.empty:
            last_dt = chunk["date"].iloc[-1]
            cur_start = last_dt + timedelta(hours=1)
        else:
            cur_start = cur_end
        time.sleep(0.2)  # be a bit polite
    if not out_chunks:
        return pd.DataFrame()
    out = pd.concat(out_chunks, ignore_index=True)
    out = out.drop_duplicates(subset=["date"]).sort_values("date").reset_index(drop=True)
    out = out[(out["date"] >= start_dt) & (out["date"] < end_dt)].reset_index(drop=True)
    return out

def fetch_data(symbol_key: str, start_dt: datetime, end_dt: datetime, provider: str = "auto") -> pd.DataFrame:
    prov = provider.lower().strip()
    tried = []

    def _try_binance():
        print(f"  [binance] {symbol_key} 1h")
        kl = fetch_binance(symbol_key, "1h", to_ms(start_dt), to_ms(end_dt))
        df = klines_to_df(kl)
        print(f"    got {len(df)} bars {df['date'].min()} → {df['date'].max()}")
        return df

    def _try_ccxt():
        print(f"  [ccxt] {symbol_key} 1h (multi-exchange)")
        df, ex_id = fetch_ccxt_any(symbol_key, start_dt, end_dt)
        if df.empty:
            print("    no data from ccxt sources")
        else:
            print(f"    got {len(df)} bars via {ex_id} {df['date'].min()} → {df['date'].max()}")
        return df

    def _try_yf():
        print(f"  [yfinance] {symbol_key} 1h (chunked)")
        df = fetch_yf_chunked(symbol_key, start_dt, end_dt)
        if df.empty:
            print("    got 0 bars from yfinance")
        else:
            print(f"    got {len(df)} bars {df['date'].min()} → {df['date'].max()}")
        return df

    order_map = {
        "binance": [_try_binance],
        "ccxt": [_try_ccxt],
        "yf": [_try_yf],
        "auto": [_try_binance, _try_ccxt, _try_yf],
    }
    fns = order_map.get(prov, order_map["auto"])

    for fn in fns:
        name = fn.__name__
        tried.append(name)
        try:
            df = fn()
            if not df.empty:
                return df
        except RuntimeError as e:
            msg = str(e)
            if "BINANCE_451" in msg or "451" in msg:
                print("    binance region-blocked (451); continuing")
            else:
                print(f"    provider error: {msg}; continuing")
        except Exception as e:
            print(f"    provider exception: {e}; continuing")

    print(f"  all providers failed (tried: {', '.join(tried)})")
    return pd.DataFrame()

# -------- Indicators --------
def atr(df: pd.DataFrame, period: int = 14) -> pd.Series:
    high = df["high"].astype(float).values
    low = df["low"].astype(float).values
    close = df["close"].astype(float).values
    prev_close = np.roll(close, 1)
    prev_close[0] = close[0]
    tr = np.maximum.reduce([
        high - low,
        np.abs(high - prev_close),
        np.abs(low - prev_close)
    ])
    atr_vals = pd.Series(tr).ewm(span=period, adjust=False).mean().values
    return pd.Series(atr_vals, index=df.index)

# -------- Strategy --------
@dataclass
class Trade:
    entry_time: datetime
    direction: int   # +1 long, -1 short
    entry_price: float
    sl: float
    tp: float
    exit_time: Optional[datetime] = None
    exit_price: Optional[float] = None
    reason: Optional[str] = None

def run_strategy(
    df: pd.DataFrame,
    min_move_bps: float = 50.0,
    sl_mult: float = 1.0,
    tp_mult: float = 1.0,
    fee_bps: float = 8.0,
    atr_period: int = 14,
) -> Dict:
    """
    Mean-reversion "hourly reversal":
      - If previous bar's return >= +threshold => short next open
      - If previous bar's return <= -threshold => long  next open
    Entry at next bar's open.
    SL/TP sized by ATR * multipliers.
    No same-bar exits: exits start from the bar AFTER entry.

    No leverage. Fees per side: equity *= (1 - fee_rate) at entry and exit.
    """
    if df.empty or len(df) < atr_period + 5:
        return {
            "equity_path": [100.0],
            "trades": [],
            "final_equity": 100.0,
            "avg_ret": 0.0,
            "med_ret": 0.0,
            "win_rate": 0.0,
        }

    df = df.copy().reset_index(drop=True)
    df["atr"] = atr(df, atr_period)
    df["ret1"] = df["close"].pct_change().fillna(0.0)

    fee_rate = fee_bps / 10000.0
    equity = 100.0
    equity_path = [equity]
    trades: List[Trade] = []
    in_pos = False
    trade: Optional[Trade] = None
    last_entry_idx = None

    for i in range(1, len(df)-1):
        row_prev = df.iloc[i-1]
        row = df.iloc[i]
        row_next = df.iloc[i+1]

        if in_pos:
            # no same-bar exit: allow exits only strictly after entry index
            if i > last_entry_idx:
                h = row["high"]; l = row["low"]
                if trade.direction == +1:
                    hit_sl = l <= trade.sl
                    hit_tp = h >= trade.tp
                else:
                    hit_sl = h >= trade.sl
                    hit_tp = l <= trade.tp
                if hit_sl and hit_tp:
                    # conservative: assume stop hit first
                    hit_tp = False
                if hit_sl or hit_tp:
                    px = trade.sl if hit_sl else trade.tp
                    trade.exit_time = row["date"]
                    trade.exit_price = px
                    trade.reason = "SL" if hit_sl else "TP"
                    gross_ret = trade.direction * (px / trade.entry_price - 1.0)
                    equity *= (1.0 - fee_rate)  # entry fee already applied at entry; keep symmetry for safety
                    equity *= (1.0 + gross_ret)
                    equity *= (1.0 - fee_rate)
                    trades.append(trade)
                    in_pos = False
                    trade = None
                    equity_path.append(equity)
                    continue

        if not in_pos:
            ret_prev_bps = (row["close"] / row_prev["close"] - 1.0) * 10000.0
            if ret_prev_bps >= min_move_bps:
                entry_px = row_next["open"]
                rng = df.loc[i, "atr"]
                sl = entry_px + sl_mult * rng
                tp = entry_px - tp_mult * rng
                trade = Trade(entry_time=row_next["date"], direction=-1, entry_price=entry_px, sl=sl, tp=tp)
                equity *= (1.0 - fee_rate)  # entry fee
                in_pos = True
                last_entry_idx = i+1
                equity_path.append(equity)
            elif ret_prev_bps <= -min_move_bps:
                entry_px = row_next["open"]
                rng = df.loc[i, "atr"]
                sl = entry_px - sl_mult * rng
                tp = entry_px + tp_mult * rng
                trade = Trade(entry_time=row_next["date"], direction=+1, entry_price=entry_px, sl=sl, tp=tp)
                equity *= (1.0 - fee_rate)
                in_pos = True
                last_entry_idx = i+1
                equity_path.append(equity)

    if in_pos and trade is not None:
        last_row = df.iloc[-1]
        px = last_row["close"]
        trade.exit_time = last_row["date"]
        trade.exit_price = px
        trade.reason = "EOD"
        gross_ret = trade.direction * (px / trade.entry_price - 1.0)
        equity *= (1.0 + gross_ret)
        equity *= (1.0 - fee_rate)
        trades.append(trade)
        equity_path.append(equity)

    rets = []
    wins = 0
    for t in trades:
        r = t.direction * (t.exit_price / t.entry_price - 1.0)
        rets.append(r)
        if r > 0:
            wins += 1

    final_eq = equity
    avg_ret = float(np.mean(rets)) if rets else 0.0
    med_ret = float(np.median(rets)) if rets else 0.0
    win_rate = (wins / len(rets) * 100.0) if rets else 0.0

    return {
        "equity_path": equity_path,
        "trades": trades,
        "final_equity": final_eq,
        "avg_ret": avg_ret,
        "med_ret": med_ret,
        "win_rate": win_rate,
    }

# -------- Grid search --------
def parse_csv_floats(s: str) -> List[float]:
    return [float(x.strip()) for x in s.split(",") if x.strip()]

def grid_search(
    df: pd.DataFrame,
    min_move_bps: float,
    sl_mults: List[float],
    tp_mults: List[float],
    fee_bps: float,
    atr_period: int = 14
) -> Tuple[Dict, Tuple[float, float], List[Tuple[float, float, float, int, float]]]:
    best = None
    best_params = None
    leaderboard = []
    for sl in sl_mults:
        for tp in tp_mults:
            res = run_strategy(
                df,
                min_move_bps=min_move_bps,
                sl_mult=sl,
                tp_mult=tp,
                fee_bps=fee_bps,
                atr_period=atr_period,
            )
            ntr = len(res["trades"])
            leaderboard.append((sl, tp, res["final_equity"], ntr, res["win_rate"]))
            if (best is None) or (res["final_equity"] > best["final_equity"]):
                best = res
                best_params = (sl, tp)
    leaderboard.sort(key=lambda x: x[2], reverse=True)
    return best, best_params, leaderboard

# -------- CLI / Main --------
def main():
    parser = argparse.ArgumentParser(description="Hourly reversal backtest (no leverage, fee-aware, no same-bar exits).")
    parser.add_argument("--symbols", type=str, default="BTCUSDT,ETHUSDT,SOLUSDT", help="Comma-separated symbols (Binance-style keys).")
    parser.add_argument("--start", type=str, default="2023-01-01", help="Start date: YYYY-MM-DD, now, today, yesterday, or -Nd/-Nm/-Ny.")
    parser.add_argument("--end", type=str, default="now", help="End date: YYYY-MM-DD, now, today, yesterday, or -Nd/-Nm/-Ny.")
    parser.add_argument("--fee-bps", type=float, default=8.0, help="Per-side trading fee in bps (e.g., 8 = 0.08%). No leverage.")
    parser.add_argument("--min-move-bps", type=float, default=50.0, help="Prior-bar absolute move threshold (bps) to trigger reversal entry.")
    parser.add_argument("--sl-mults", type=parse_csv_floats, default=parse_csv_floats("0.75,1.00,1.50,2.00"), help="CSV of ATR SL multipliers.")
    parser.add_argument("--tp-mults", type=parse_csv_floats, default=parse_csv_floats("0.75,1.25,1.50,2.00"), help="CSV of ATR TP multipliers.")
    parser.add_argument("--atr-period", type=int, default=14, help="ATR period.")
    parser.add_argument("--provider", type=str, default="auto", help="Data source: auto|binance|ccxt|yf")
    args = parser.parse_args()

    syms = [s.strip().upper() for s in args.symbols.split(",") if s.strip()]
    start_dt = parse_date_like(args.start)
    end_dt_inclusive = parse_date_like(args.end)
    # make end exclusive for fetching ranges
    end_dt = end_dt_inclusive + timedelta(days=1) if end_dt_inclusive.hour == 0 and end_dt_inclusive.minute == 0 and end_dt_inclusive.second == 0 else end_dt_inclusive
    end_dt = min(end_dt, datetime.now(tz=UTC))  # don't ask the future

    print("Running hourly reversal backtest...")
    results_summary = []

    for s in syms:
        print(f"\n=== {s} ===")
        df = fetch_data(s, start_dt, end_dt, provider=args.provider)
        if df.empty:
            print("  No data. Skipping.")
            results_summary.append([s, 0, 0.0, 0.0, 0.0, 100.0, "n/a"])
            continue

        best, (best_sl, best_tp), board = grid_search(
            df,
            min_move_bps=args.min_move_bps,
            sl_mults=args.sl_mults,
            tp_mults=args.tp_mults,
            fee_bps=args.fee_bps,
            atr_period=args.atr_period
        )

        ntr = len(best["trades"])
        print(f"  Optimal (fee {args.fee_bps} bps/side, threshold {args.min_move_bps} bps):")
        print(f"    SL x ATR = {best_sl:.2f}, TP x ATR = {best_tp:.2f}")
        print(f"    Trades: {ntr}, Win%: {best['win_rate']:.1f}%, AvgR: {best['avg_ret']*100:.2f}%, MedR: {best['med_ret']*100:.2f}%")
        print(f"    Final equity: ${best['final_equity']:.2f}")

        topn = min(5, len(board))
        hdr = ["SLxATR", "TPxATR", "FinalEq", "#Trades", "Win%"]
        rows = [(sl, tp, f"{feq:.2f}", n, f"{wr:.1f}") for (sl, tp, feq, n, wr) in board[:topn]]
        print(tabulate(rows, headers=hdr, tablefmt="github"))

        results_summary.append([s, ntr, best["win_rate"], best["avg_ret"]*100.0, best["med_ret"]*100.0, best["final_equity"], f"{best_sl:.2f}/{best_tp:.2f}"])

    print("\n=== Summary ===")
    hdr = ["Symbol", "#Trades", "Win%", "AvgR %", "MedR %", "FinalEq $", "Best SL/TP (xATR)"]
    print(tabulate(results_summary, headers=hdr, tablefmt="github"))

if __name__ == "__main__":
    main()
