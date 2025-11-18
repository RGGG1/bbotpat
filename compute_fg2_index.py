"""
Compute Hive Mind Index (HMI) — Binance-only version.

Uses:
- Binance spot OHLCV (BTCUSDT)
- Binance futures perp OHLCV + open interest

We compute daily HMI and save to output/fg2_daily.csv
(HMI is stored in the FG_lite column for compatibility).

Compared to the previous version:
- Coinbase + Coinalyze are no longer used.
- We rely only on Binance public APIs for prices/volumes/OI.
"""

import os
import time
from datetime import datetime, timedelta

import requests
import pandas as pd
import numpy as np

# ---------------- CONFIG ----------------

BINANCE_SPOT = "https://api.binance.com"
BINANCE_FUT  = "https://fapi.binance.com"

SYMBOL_SPOT  = "BTCUSDT"  # spot pair
SYMBOL_PERP  = "BTCUSDT"  # futures perp pair

# Use a rolling window of ~2 years for vol; Binance OI history covers ~500d.
END_DATE   = datetime.utcnow().date().isoformat()
START_DATE = (datetime.utcnow().date() - timedelta(days=730)).isoformat()

OUT_CSV = "output/fg2_daily.csv"
os.makedirs("output", exist_ok=True)


# ---------------- UTIL ----------------

def iso_to_unix(date_str: str) -> int:
    return int(datetime.strptime(date_str, "%Y-%m-%d").timestamp())


def unix_ms_to_date(ms: int):
    return datetime.utcfromtimestamp(ms / 1000.0).date()


# ---------------- BINANCE HELPERS ----------------

def bn_spot_get(path, params=None, timeout=30, sleep=0.1, max_retries=5):
    if params is None:
        params = {}
    url = BINANCE_SPOT + path
    last_err = ""
    for attempt in range(1, max_retries + 1):
        try:
            r = requests.get(url, params=params, timeout=timeout)
        except requests.RequestException as e:
            last_err = str(e)
        else:
            if r.status_code == 200:
                time.sleep(sleep)
                return r.json()
            if r.status_code in (429, 418, 500, 502, 503, 504):
                last_err = r.text[:300]
            else:
                raise RuntimeError(f"Binance spot error {r.status_code}: {r.text[:300]}")
        if attempt < max_retries:
            delay = sleep * attempt
            print(f"[Binance spot] retry {attempt}/{max_retries} in {delay:.2f}s…")
            time.sleep(delay)
    raise RuntimeError(f"Binance spot error after retries: {last_err}")


def bn_fut_get(path, params=None, timeout=30, sleep=0.1, max_retries=5):
    if params is None:
        params = {}
    url = BINANCE_FUT + path
    last_err = ""
    for attempt in range(1, max_retries + 1):
        try:
            r = requests.get(url, params=params, timeout=timeout)
        except requests.RequestException as e:
            last_err = str(e)
        else:
            if r.status_code == 200:
                time.sleep(sleep)
                return r.json()
            if r.status_code in (429, 418, 500, 502, 503, 504):
                last_err = r.text[:300]
            else:
                raise RuntimeError(f"Binance fut error {r.status_code}: {r.text[:300]}")
        if attempt < max_retries:
            delay = sleep * attempt
            print(f"[Binance fut] retry {attempt}/{max_retries} in {delay:.2f}s…")
            time.sleep(delay)
    raise RuntimeError(f"Binance fut error after retries: {last_err}")


# ---------------- 1) SPOT DATA ----------------

def fetch_spot_ohlcv(start_date, end_date):
    """
    Fetch BTCUSDT daily klines from Binance spot in a single request.

    /api/v3/klines returns:
    [
      openTime, open, high, low, close, volume,
      closeTime, quoteAssetVolume, numberOfTrades,
      takerBuyBaseVolume, takerBuyQuoteVolume, ignore
    ]
    """
    start_ts = iso_to_unix(start_date)
    end_ts   = iso_to_unix(end_date) + 86400

    # Request up to 1000 daily candles (enough for ~3y)
    data = bn_spot_get(
        "/api/v3/klines",
        params={"symbol": SYMBOL_SPOT, "interval": "1d", "limit": 1000},
    )

    rows = []
    for k in data:
        open_time_ms = k[0]
        close_price  = float(k[4])
        volume       = float(k[5])
        d = unix_ms_to_date(open_time_ms)
        if not (datetime.strptime(start_date, "%Y-%m-%d").date()
                <= d
                <= datetime.strptime(end_date, "%Y-%m-%d").date()):
            continue
        rows.append((d, close_price, volume))

    df = pd.DataFrame(rows, columns=["date", "spot_close", "spot_volume"])
    return df.drop_duplicates("date").sort_values("date")


# ---------------- 2) PERP VOLUME + OI ----------------

def fetch_perp_volume_daily(start_date, end_date):
    """
    Fetch BTCUSDT perp daily klines from Binance futures for volume.
    """
    data = bn_fut_get(
        "/fapi/v1/klines",
        params={"symbol": SYMBOL_PERP, "interval": "1d", "limit": 1000},
    )
    rows = []
    for k in data:
        open_time_ms = k[0]
        # close = float(k[4])  # not needed here
        volume = float(k[5])   # base asset volume
        d = unix_ms_to_date(open_time_ms)
        if not (datetime.strptime(start_date, "%Y-%m-%d").date()
                <= d
                <= datetime.strptime(end_date, "%Y-%m-%d").date()):
            continue
        rows.append((d, volume))

    df = pd.DataFrame(rows, columns=["date", "perp_volume"])
    return df.drop_duplicates("date").sort_values("date")


def fetch_open_interest_daily():
    """
    Fetch BTCUSDT perp open interest history from Binance futures.

    /futures/data/openInterestHist supports period=1d and limit<=500.
    That gives ~500 days of daily OI, which is enough for the HMI
    rolling computations.
    """
    data = bn_fut_get(
        "/futures/data/openInterestHist",
        params={"symbol": SYMBOL_PERP, "period": "1d", "limit": 500},
    )
    rows = []
    for item in data:
        ts_ms = int(item["timestamp"])
        # Prefer sumOpenInterestValue (USD notional) if present
        oi_val = float(item.get("sumOpenInterestValue")
                       or item.get("sumOpenInterest")
                       or 0.0)
        d = unix_ms_to_date(ts_ms)
        rows.append((d, oi_val))

    df = pd.DataFrame(rows, columns=["date", "oi_usd"])
    return df.drop_duplicates("date").sort_values("date")


# ---------------- SCORING ----------------

def clip01(x):
    return np.minimum(1, np.maximum(0, x))


def rolling_minmax(series, window=365, lower_q=0.05, upper_q=0.95):
    low = series.rolling(window).quantile(lower_q)
    high = series.rolling(window).quantile(upper_q)
    return low, high


def compute_fg_lite(df):
    eps = 1e-9

    # volatility
    df["log_ret"] = np.log(df["spot_close"] / df["spot_close"].shift(1))
    df["RV_30"] = df["log_ret"].rolling(30).std() * np.sqrt(365)
    df["RV_90"] = df["log_ret"].rolling(90).std() * np.sqrt(365)

    V_raw = df["RV_90"] / (df["RV_30"] + eps)
    V_low, V_high = rolling_minmax(V_raw)
    V_score = 100 * clip01((V_raw - V_low) / (V_high - V_low + eps))

    # open interest
    OI = df["oi_usd"]
    OI_low, OI_high = rolling_minmax(OI)
    OI_score = 100 * clip01((OI - OI_low) / (OI_high - OI_low + eps))

    # spot vs perp dominance
    df["perp_frac"] = df["perp_volume"] / (df["perp_volume"] + df["spot_volume"] + eps)
    PF_low, PF_high = rolling_minmax(df["perp_frac"])
    SP_score = 100 * clip01((df["perp_frac"] - PF_low) / (PF_high - PF_low + eps))

    FG_lite = (
        0.50 * OI_score +
        0.30 * SP_score +
        0.20 * V_score
    )

    out = df.copy()
    out["FG_lite"]     = FG_lite
    out["FG_vol"]      = V_score
    out["FG_oi"]       = OI_score
    out["FG_spotperp"] = SP_score
    return out


# ---------------- MAIN ----------------

def main():
    print(f"Fetching Binance BTCUSDT spot OHLCV… ({START_DATE} → {END_DATE})")
    cb_df = fetch_spot_ohlcv(START_DATE, END_DATE)

    print("Fetching Binance BTCUSDT perp volume…")
    vol_df = fetch_perp_volume_daily(START_DATE, END_DATE)

    print("Fetching Binance BTCUSDT perp open interest history…")
    oi_df = fetch_open_interest_daily()

    print("Merging datasets…")
    df = cb_df.merge(vol_df, on="date", how="inner")
    df = df.merge(oi_df, on="date", how="inner")
    df = df.sort_values("date")

    print("Computing HMI (FG_lite)…")
    fg_df = compute_fg_lite(df)

    fg_df.to_csv(OUT_CSV, index=False)
    print(f"Saved HMI data to {OUT_CSV}")
    print(fg_df[['date', 'FG_lite', 'FG_vol', 'FG_oi', 'FG_spotperp']].tail())


if __name__ == "__main__":
    main()
    
