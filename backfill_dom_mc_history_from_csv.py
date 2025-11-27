#!/usr/bin/env python3
"""
backfill_dom_mc_history_from_csv.py

One-off bootstrap of dom_mc_history.json using locally stored
CoinGecko export files (CSV with columns: snapped_at, price, market_cap, ...).

We:
  - load daily market_cap for each token (BTC, ETH, BNB, SOL, DOGE, SUI, UNI, USDC, USDT)
  - align by calendar date
  - keep a rolling 730-day window (or less if the token is younger)
  - write dom_mc_history.json in the format:

      {
        "series": [
          {
            "date": "YYYY-MM-DD",
            "mc": {
              "BTC": ...,
              "ETH": ...,
              ...
            }
          },
          ...
        ]
      }

After running this once, your existing update_dom_mc_history.py
and update_token_dom_ranges.py can keep it fresh.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, List, Optional

import pandas as pd

ROOT = Path(".")
HIST_DIR = ROOT / "history"
OUT_FILE = ROOT / "dom_mc_history.json"
DOCS_OUT = ROOT / "docs" / "dom_mc_history.json"

# --- adjust these filenames to match what you actually have in /root/bbotpat/history ---
# The keys are the symbols used everywhere else (BTC, ETH, ...).
TOKEN_FILES: Dict[str, str] = {
    "BTC": "btc-usd-max.xls",     # or "btc-usd-max(1).xls" if that is your actual filename
    "ETH": "eth-usd-max.xls",
    "BNB": "bnb-usd-max.xls",
    "SOL": "sol-usd-max.xls",
    "DOGE": "doge-usd-max.xls",
    "SUI": "sui-usd-max.xls",
    "UNI": "Uni-usd-max.xls",
    "USDC": "usdc-usd-max.xls",
    "USDT": "usdt-usd-max.xls",
    # "TON": "ton-usd-max.xls",   # add later if/when you export TON
}

WINDOW_DAYS = 730  # rolling window length


@dataclass
class SeriesSummary:
    symbol: str
    n_days: int
    first_date: Optional[str]
    last_date: Optional[str]


def load_token_series(symbol: str, filename: str) -> Dict[datetime.date, float]:
    """
    Load market_cap time series for a single token from a CSV/“XLS” file
    that actually contains CSV data with columns including 'snapped_at' and 'market_cap'.
    Returns a dict: {date: market_cap}.
    """
    path = HIST_DIR / filename
    if not path.exists():
        print(f"[WARN] File for {symbol} not found: {path}")
        return {}

    print(f"[INFO] Loading {symbol} from {path}")
    # Your exported files are CSV despite .xls extension
    df = pd.read_csv(path)

    # Basic sanity
    if "snapped_at" not in df.columns or "market_cap" not in df.columns:
        print(f"[WARN] {path} does not have snapped_at/market_cap columns; skipping.")
        return {}

    # Parse date
    df["snapped_at"] = pd.to_datetime(df["snapped_at"], errors="coerce", utc=True)
    df = df.dropna(subset=["snapped_at", "market_cap"])

    # Convert to plain date
    df["date"] = df["snapped_at"].dt.date

    # Group by date (if multiple rows per day, take the last one)
    grouped = df.groupby("date", as_index=False)["market_cap"].last()

    series: Dict[datetime.date, float] = {}
    for _, row in grouped.iterrows():
        dt = row["date"]
        mc = float(row["market_cap"])
        # skip obviously broken values
        if mc <= 0:
            continue
        series[dt] = mc

    return series


def build_dom_history() -> Dict[str, List[Dict]]:
    """
    Build dom_mc_history payload:
      { "series": [ { "date": "...", "mc": { sym: mc, ... } }, ... ] }
    using a rolling 730-day window (or less, depending on token age).
    """
    per_token: Dict[str, Dict[datetime.date, float]] = {}

    # Load all series
    for sym, fname in TOKEN_FILES.items():
        s = load_token_series(sym, fname)
        if not s:
            print(f"[WARN] No data loaded for {sym}")
        per_token[sym] = s

    # Determine global max date across all tokens
    all_dates = set()
    for s in per_token.values():
        all_dates.update(s.keys())

    if not all_dates:
        raise SystemExit("No dates found in any token series; aborting.")

    max_date = max(all_dates)
    cutoff_date = max_date - timedelta(days=WINDOW_DAYS - 1)

    print(f"[INFO] Global max date: {max_date.isoformat()}")
    print(f"[INFO] Using rolling window: {cutoff_date.isoformat()} .. {max_date.isoformat()} ({WINDOW_DAYS} days)")

    # Build unified date list
    dates_sorted = sorted(d for d in all_dates if d >= cutoff_date)

    # Build series array
    out_series: List[Dict] = []
    for d in dates_sorted:
        mc_map: Dict[str, float] = {}
        for sym, s in per_token.items():
            if d in s:
                mc_map[sym] = s[d]
        if not mc_map:
            # if no token has data for this date, skip
            continue
        out_series.append(
            {
                "date": d.isoformat(),
                "mc": mc_map,
            }
        )

    return {"series": out_series}


def summarise(history: Dict[str, List[Dict]]) -> List[SeriesSummary]:
    """
    Build a per-token summary: how many days, first & last date.
    """
    series = history.get("series", [])
    per_token_dates: Dict[str, List[datetime.date]] = {}

    for row in series:
        d_str = row.get("date")
        try:
            d = datetime.fromisoformat(d_str).date()
        except Exception:
            continue
        mc = row.get("mc", {})
        if not isinstance(mc, dict):
            continue
        for sym in mc.keys():
            per_token_dates.setdefault(sym, []).append(d)

    summaries: List[SeriesSummary] = []
    for sym in sorted(per_token_dates.keys()):
        dates = sorted(per_token_dates[sym])
        n = len(dates)
        first_date = dates[0].isoformat() if dates else None
        last_date = dates[-1].isoformat() if dates else None
        summaries.append(SeriesSummary(symbol=sym, n_days=n, first_date=first_date, last_date=last_date))
    return summaries


def main():
    history = build_dom_history()

    # Write to root and docs/
    OUT_FILE.write_text(json.dumps(history, indent=2))
    DOCS_OUT.parent.mkdir(parents=True, exist_ok=True)
    DOCS_OUT.write_text(json.dumps(history, indent=2))

    print(f"[OK] Wrote dom_mc_history.json with {len(history['series'])} daily rows.")
    print(f"     Root: {OUT_FILE}")
    print(f"     Docs: {DOCS_OUT}")

    # Print a small summary
    print("\nPer-token coverage in window:")
    summaries = summarise(history)
    for s in summaries:
        print(f"  {s.symbol}: {s.n_days} days  [{s.first_date} .. {s.last_date}]")


if __name__ == "__main__":
    main()
