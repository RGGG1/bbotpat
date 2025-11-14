"""
Compute Hive Mind Index (HMI) — previously FG_lite.

Uses:
- Coinbase spot OHLCV (BTC-USD)
- Coinalyze perp OHLCV + open interest

We compute daily HMI and save to output/fg2_daily.csv
(HMI is stored in the FG_lite column for compatibility).
"""

import os
import time
from datetime import datetime, timedelta

import requests
import pandas as pd
import numpy as np

# ---------------- CONFIG ----------------

COINBASE_BASE   = "https://api.exchange.coinbase.com"
COINALYZE_BASE  = "https://api.coinalyze.net/v1"

COINALYZE_API_KEY = os.getenv("COINALYZE_API_KEY")

SYMBOL_CB    = "BTC-USD"          # Coinbase product
SYMBOL_PERP  = "BTCUSDT_PERP.A"   # Coinalyze aggregated BTC perp symbol

# Use a rolling window of ~2 years to support 365d rolling min/max etc.
END_DATE     = datetime.utcnow().date().isoformat()
START_DATE   = (datetime.utcnow().date() - timedelta(days=730)).isoformat()

OUT_CSV = "output/fg2_daily.csv"
os.makedirs("output", exist_ok=True)


# ---------------- UTIL ----------------

def iso_to_unix(date_str: str) -> int:
    return int(datetime.strptime(date_str, "%Y-%m-%d").timestamp())


def unix_to_date(ts: int):
    return datetime.utcfromtimestamp(ts).date()


# ---------------- COINBASE (with retry) ----------------

def cb_get(path, params=None, sleep=0.25, max_retries=5):
    """
    Coinbase GET with retry for 5xx errors.
    """
    if params is None:
        params = {}

    url = COINBASE_BASE + path
    last_err = ""

    for attempt in range(1, max_retries + 1):
        try:
            r = requests.get(url, params=params, timeout=30)
        except requests.RequestException as e:
            last_err = str(e)
            time.sleep(sleep * attempt)
            continue

        if r.status_code == 200:
            time.sleep(sleep)
            return r.json()

        # Retry on 5xx server errors
        if 500 <= r.status_code < 600:
            last_err = r.text[:300]
            if attempt < max_retries:
                time.sleep(sleep * attempt)
                continue

        last_err = r.text[:300]
        break

    raise RuntimeError(
        f"Coinbase error after {max_retries} attempts: {last_err}"
    )


# ---------------- COINALYZE ----------------

def coinalyze_get(path, params=None, sleep=0.25):
    if COINALYZE_API_KEY is None:
        raise RuntimeError("COINALYZE_API_KEY not set in environment.")

    if params is None:
        params = {}

    url = COINALYZE_BASE + path
    params = {**params, "api_key": COINALYZE_API_KEY}

    r = requests.get(url, params=params, timeout=30)
    if r.status_code != 200:
        raise RuntimeError(f"Coinalyze error {r.status_code}: {r.text[:300]}")
    time.sleep(sleep)
    return r.json()


# ---------------- 1) SPOT DATA ----------------

def fetch_cb_btc_ohlcv(start_date, end_date):
    """
    Fetch BTC-USD daily candles from Coinbase in <=300-candle chunks.

    /candles returns [time, low, high, open, close, volume].
    """
    granularity = 86400      # 1d candles
    max_points = 300

    start_unix = iso_to_unix(start_date)
    end_unix   = iso_to_unix(end_date) + 86400

    rows = []
    cur_start = start_unix

    while cur_start < end_unix:
        cur_end = cur_start + granularity * max_points
        if cur_end > end_unix:
            cur_end = end_unix

        params = {
            "granularity": granularity,
            "start": datetime.utcfromtimestamp(cur_start).isoformat() + "Z",
            "end": datetime.utcfromtimestamp(cur_end).isoformat() + "Z",
        }

        data = cb_get(f"/products/{SYMBOL_CB}/candles", params=params)

        if not data:
            break

        for row in data:
            ts, low, high, open_, close, volume = row
            rows.append((unix_to_date(ts), close, volume))

        # Coinbase returns candles newest→oldest; find oldest ts
        oldest_ts = min(r[0] for r in data)
        cur_start = oldest_ts + granularity
        if cur_start <= start_unix:
            break
        start_unix = cur_start

    df = pd.DataFrame(rows, columns=["date", "spot_close", "spot_volume"])
    return df.drop_duplicates("date").sort_values("date")


# ---------------- 2) PERP DATA ----------------

def fetch_coinalyze_oi_and_perp_volume(start_date, end_date):

    start_unix = iso_to_unix(start_date)
    end_unix   = iso_to_unix(end_date)

    # open interest
    js = coinalyze_get(
        "/open-interest-history",
        params={
            "symbols": SYMBOL_PERP,
            "interval": "daily",
            "from": start_unix,
            "to": end_unix,
            "convert_to_usd": "true",
        },
    )

    oi_rows = []
    for item in js:
        if item.get("symbol") != SYMBOL_PERP:
            continue
        for h in item.get("history", []):
            oi_rows.append((unix_to_date(int(h["t"])), float(h["c"])))

    oi_df = pd.DataFrame(oi_rows, columns=["date", "oi_usd"])

    # perp volume
    js2 = coinalyze_get(
        "/ohlcv-history",
        params={
            "symbols": SYMBOL_PERP,
            "interval": "daily",
            "from": start_unix,
            "to": end_unix,
        },
    )

    vol_rows = []
    for item in js2:
        if item.get("symbol") != SYMBOL_PERP:
            continue
        for h in item.get("history", []):
            vol_rows.append((unix_to_date(int(h["t"])), float(h["v"])))

    vol_df = pd.DataFrame(vol_rows, columns=["date", "perp_volume"])

    return oi_df.merge(vol_df, on="date", how="inner").sort_values("date")


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
    print(f"Fetching Coinbase BTC-USD OHLCV… ({START_DATE} → {END_DATE})")
    cb_df = fetch_cb_btc_ohlcv(START_DATE, END_DATE)

    print("Fetching Coinalyze BTC perp OI & volumes…")
    cl_df = fetch_coinalyze_oi_and_perp_volume(START_DATE, END_DATE)

    print("Merging datasets…")
    df = cb_df.merge(cl_df, on="date", how="inner").sort_values("date")

    print("Computing HMI (FG_lite)…")
    fg_df = compute_fg_lite(df)

    fg_df.to_csv(OUT_CSV, index=False)
    print(f"Saved HMI data to {OUT_CSV}")
    print(fg_df[['date','FG_lite','FG_vol','FG_oi','FG_spotperp']].tail())


if __name__ == "__main__":
    main()
        
