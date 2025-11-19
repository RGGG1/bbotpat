#!/usr/bin/env python3
"""
backfill_hmi_history.py

One-time backfill for HMI:

- Fetch ~730d of global BTC perp OI + perp volume from Coinalyze.
- Fetch ~730d of BTCUSDT spot from Binance.
- Fetch today's Binance perp OI from Binance.
- Compute a constant Binance share:

    s = (Binance OI today in USD) / (Global OI today in USD)

- For each past day, synthesize a "Binance-like" OI + perp volume:

    bn_oi_usd(t)   = s * global_oi_usd(t)
    bn_perp_vol(t) = s * global_perp_vol(t)

- Save daily history to data/hmi_oi_history.csv with columns:
    date, spot_close, spot_volume, perp_volume, oi_usd
"""

import os
import time
from datetime import datetime, timedelta
from pathlib import Path

import requests
import pandas as pd
import numpy as np

COINALYZE_BASE = "https://api.coinalyze.net/v1"
BINANCE_SPOT_BASE = "https://api.binance.com"
BINANCE_FUTURES_BASE = "https://fapi.binance.com"

COINALYZE_API_KEY = os.getenv("COINALYZE_API_KEY")
if COINALYZE_API_KEY is None:
    raise RuntimeError("COINALYZE_API_KEY not set in environment.")

SYMBOL_PERP_COINALYZE = "BTCUSDT_PERP.A"  # aggregated BTC perpetual
SPOT_SYMBOL = "BTCUSDT"

# 730 days back from today (UTC)
END_DATE = datetime.utcnow().date()
START_DATE = END_DATE - timedelta(days=730)

OUT_DIR = Path("data")
OUT_DIR.mkdir(exist_ok=True)
OUT_CSV = OUT_DIR / "hmi_oi_history.csv"


# -------- UTIL --------

def iso_to_unix(date_str: str) -> int:
    return int(datetime.strptime(date_str, "%Y-%m-%d").timestamp())


def unix_to_date(ts: int):
    return datetime.utcfromtimestamp(ts).date()


def coinalyze_get(path: str, params=None, sleep: float = 0.25):
    if params is None:
        params = {}

    url = COINALYZE_BASE + path
    params = {**params, "api_key": COINALYZE_API_KEY}

    r = requests.get(url, params=params, timeout=60)
    if r.status_code != 200:
        raise RuntimeError(f"Coinalyze error {r.status_code}: {r.text[:300]}")
    time.sleep(sleep)
    return r.json()


def bn_spot_get_klines(symbol: str, interval: str, limit: int = 1000):
    url = BINANCE_SPOT_BASE + "/api/v3/klines"
    params = {
        "symbol": symbol,
        "interval": interval,
        "limit": limit,
    }
    r = requests.get(url, params=params, timeout=60)
    if r.status_code != 200:
        raise RuntimeError(f"Binance spot error {r.status_code}: {r.text[:300]}")
    return r.json()


def bn_futures_get(path: str, params=None):
    if params is None:
        params = {}
    url = BINANCE_FUTURES_BASE + path
    r = requests.get(url, params=params, timeout=60)
    if r.status_code != 200:
        raise RuntimeError(f"Binance futures error {r.status_code}: {r.text[:300]}")
    return r.json()


# -------- BACKFILL COMPONENTS --------

def fetch_global_oi_and_perp_volume(start_date, end_date):
    """
    Use Coinalyze to fetch ~730d global perp OI and perp volume for BTCUSDT_PERP.A
    at daily frequency.
    """
    start_unix = iso_to_unix(start_date)
    end_unix = iso_to_unix(end_date)

    # Open interest history (USD)
    js_oi = coinalyze_get(
        "/open-interest-history",
        params={
            "symbols": SYMBOL_PERP_COINALYZE,
            "interval": "daily",
            "from": start_unix,
            "to": end_unix,
            "convert_to_usd": "true",
        },
    )

    oi_rows = []
    for item in js_oi:
        if item.get("symbol") != SYMBOL_PERP_COINALYZE:
            continue
        for h in item.get("history", []):
            d = unix_to_date(int(h["t"]))
            oi_usd = float(h["c"])
            oi_rows.append((d, oi_usd))

    oi_df = pd.DataFrame(oi_rows, columns=["date", "global_oi_usd"])

    # Perp volume (Coinalyze's 'v' as notional volume)
    js_vol = coinalyze_get(
        "/ohlcv-history",
        params={
            "symbols": SYMBOL_PERP_COINALYZE,
            "interval": "daily",
            "from": start_unix,
            "to": end_unix,
        },
    )

    vol_rows = []
    for item in js_vol:
        if item.get("symbol") != SYMBOL_PERP_COINALYZE:
            continue
        for h in item.get("history", []):
            d = unix_to_date(int(h["t"]))
            v = float(h["v"])
            vol_rows.append((d, v))

    vol_df = pd.DataFrame(vol_rows, columns=["date", "global_perp_vol"])

    df = oi_df.merge(vol_df, on="date", how="inner").sort_values("date")
    df["date"] = df["date"].astype("datetime64[ns]").dt.date
    return df


def fetch_spot_history_from_binance(start_date, end_date):
    """
    Fetch ~730d spot daily candles for BTCUSDT from Binance.
    Binance allows up to 1000 klines per request, so we can fetch in one go.
    """
    klines = bn_spot_get_klines(SPOT_SYMBOL, "1d", limit=1000)

    rows = []
    for k in klines:
        # k: [open_time, open, high, low, close, volume, close_time, quote_volume, ...]
        open_time_ms = k[0]
        date = datetime.utcfromtimestamp(open_time_ms / 1000.0).date()
        if not (start_date <= date <= end_date):
            continue
        close = float(k[4])
        quote_volume = float(k[7])  # quote asset volume
        rows.append((date, close, quote_volume))

    df = pd.DataFrame(rows, columns=["date", "spot_close", "spot_volume"])
    df["date"] = df["date"].astype("datetime64[ns]").dt.date
    df = df.drop_duplicates("date").sort_values("date")
    return df


def fetch_today_binance_oi_usd():
    """
    Fetch current Binance BTCUSDT perps open interest in USD.

    We'll get the latest spot price and multiply by openInterest (contracts).
    """
    # Current spot price
    spot_kl = bn_spot_get_klines(SPOT_SYMBOL, "1d", limit=1)
    if not spot_kl:
        raise RuntimeError("No BTCUSDT spot kline returned for OI scaling.")
    last_k = spot_kl[-1]
    spot_close = float(last_k[4])

    # Current perps OI (in contracts)
    js = bn_futures_get("/fapi/v1/openInterest", params={"symbol": SPOT_SYMBOL})
    oi_contracts = float(js["openInterest"])
    oi_usd = oi_contracts * spot_close
    return oi_usd


def build_backfill():
    start_str = START_DATE.isoformat()
    end_str = END_DATE.isoformat()
    print(f"[backfill] Global OI range: {start_str} → {end_str}")

    df_glob = fetch_global_oi_and_perp_volume(start_str, end_str)
    if df_glob.empty:
        raise RuntimeError("No global OI/perp data returned from Coinalyze.")

    df_spot = fetch_spot_history_from_binance(START_DATE, END_DATE)
    if df_spot.empty:
        raise RuntimeError("No spot data returned from Binance.")

    df = df_spot.merge(df_glob, on="date", how="inner").sort_values("date")
    if df.empty:
        raise RuntimeError("No overlapping dates between spot and global OI.")

    print(f"[backfill] Effective date range: {df['date'].iloc[0]} → {df['date'].iloc[-1]}")
    print(f"[backfill] Rows: {len(df)}")

    # Compute Binance share based on "today"
    global_oi_today = df["global_oi_usd"].iloc[-1]
    if not np.isfinite(global_oi_today) or global_oi_today <= 0:
        raise RuntimeError("Invalid global_oi_usd for latest day; cannot compute Binance share.")

    bn_oi_today = fetch_today_binance_oi_usd()
    s = bn_oi_today / global_oi_today
    print(f"[backfill] Estimated Binance share of global OI today: {s:.3f}")

    # Synthesize Binance-like OI + perp volume for the entire history
    df["oi_usd"] = s * df["global_oi_usd"]
    df["perp_volume"] = s * df["global_perp_vol"]

    out = df[["date", "spot_close", "spot_volume", "perp_volume", "oi_usd"]].copy()
    out["date"] = out["date"].astype("datetime64[ns]").dt.date
    out = out.sort_values("date")

    OUT_CSV.parent.mkdir(exist_ok=True)
    out.to_csv(OUT_CSV, index=False)
    print(f"[backfill] Saved backfilled HMI history to {OUT_CSV}")
    print(out.tail())


def main():
    print(f"[backfill] START_DATE={START_DATE}, END_DATE={END_DATE}")
    build_backfill()


if __name__ == "__main__":
    main()
