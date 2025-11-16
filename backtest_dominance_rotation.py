#!/usr/bin/env python3
"""
'Hive' equity updater.

- BTC vs ALT bucket (ETH+SOL+BNB)
- Uses CoinGecko for daily prices/market caps (last ~365d)
- Uses HMI from output/fg2_daily.csv (stored as FG_lite)

Capital assumptions:

- Start with $100 only, entirely in stables.
- Start date: 2025-11-10 (first live Hive allocation day).
- No DCA, no further capital added.
- BTC-only benchmark also starts with $100 on the same date and just holds BTC.

We use the intersection of:
    * desired date range (2025-11-10 → today)
    * HMI (fg2) date range
If there is no overlap, we exit gracefully and leave the previous
equity_curve_fg_dom.csv untouched.
"""

import os
import time
import json
from datetime import datetime
from pathlib import Path

import requests
import pandas as pd
import numpy as np

COINGECKO_BASE = "https://api.coingecko.com/api/v3"

# Live start date for Hive:
DESIRED_START_DATE = "2025-11-10"
DESIRED_END_DATE   = datetime.utcnow().date().isoformat()

OUT_CSV_EQUITY = "output/equity_curve_fg_dom.csv"
DOM_BANDS_JSON = Path("dom_bands_latest.json")
os.makedirs("output", exist_ok=True)

GREED_STABLE_THRESHOLD = 77.0  # HMI >= 77 => fully stables

INITIAL_CAPITAL = 100.0

RISK_IDS = ["bitcoin", "ethereum", "solana", "binancecoin"]


# ---------- HELPERS ----------

def cg_get(path, params=None, sleep=1.2):
    if params is None:
        params = {}
    url = COINGECKO_BASE + path
    r = requests.get(url, params=params, timeout=60)
    if r.status_code != 200:
        raise RuntimeError(f"CoinGecko error {r.status_code}: {r.text[:300]}")
    time.sleep(sleep)
    return r.json()


def fetch_cg_ohlc_and_mc(coin_id, start_date, end_date):
    """
    Use /market_chart to get daily prices + market caps.

    We pass `days=365` (CoinGecko free limit), then filter records
    to [start_date, end_date].
    """
    start_dt = datetime.strptime(start_date, "%Y-%m-%d").date()
    end_dt   = datetime.strptime(end_date, "%Y-%m-%d").date()

    js = cg_get(
        f"/coins/{coin_id}/market_chart",
        params={"vs_currency": "usd", "days": "365"}
    )
    rows = []
    prices = js.get("prices", [])
    mcs    = js.get("market_caps", [])

    mc_map = {}
    for ts, mc in mcs:
        d = datetime.utcfromtimestamp(ts / 1000.0).date()
        mc_map[d] = mc

    for ts, price in prices:
        d = datetime.utcfromtimestamp(ts / 1000.0).date()
        if not (start_dt <= d <= end_dt):
            continue
        mc = mc_map.get(d, np.nan)
        rows.append((d, price, mc))

    df = pd.DataFrame(rows, columns=["date", f"{coin_id}_price", f"{coin_id}_mc"])
    df = df.drop_duplicates("date").sort_values("date")
    return df


def load_hmi(path="output/fg2_daily.csv"):
    """
    Load HMI (FG_lite) and return df, min_date, max_date.
    """
    if not os.path.exists(path):
        raise RuntimeError("HMI file not found; run compute_fg2_index.py first.")

    df = pd.read_csv(path, parse_dates=["date"])
    if df.empty:
        raise RuntimeError("HMI file is empty; run compute_fg2_index.py first.")

    df["date"] = df["date"].dt.date
    df = df.sort_values("date")

    dmin = df["date"].min()
    dmax = df["date"].max()

    # FG_lite column is our HMI
    df = df[["date", "FG_lite"]].rename(columns={"FG_lite": "HMI"})
    return df, dmin, dmax


def compute_dynamic_dom_bands(dom_series: pd.Series):
    """
    Compute dynamic dominance bands from a BTC dominance series in [0,1].

    Design (quantile-based, 35% / 30% / 35% mass split):
      - Bottom 35%  of dominance h
      
