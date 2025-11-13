"""
Compute Fear & Greed 2.0 index (FG2) using:

- Coinbase: BTC-USD daily OHLCV (for price & spot volume)
- Coinglass: BTC derivatives data (OI & perp volume, spot volume)
- CoinMarketCap: global total market cap & stablecoin market cap

Outputs a CSV with daily FG2 and component scores.
"""

import os
import time
from datetime import datetime, timedelta

import requests
import pandas as pd
import numpy as np

# ---------------- CONFIG ----------------

COINBASE_BASE   = "https://api.exchange.coinbase.com"
COINGLASS_BASE  = "https://open-api.coinglass.com/api"
CMC_BASE        = "https://pro-api.coinmarketcap.com"

CMC_API_KEY       = os.getenv("CMC_API_KEY")
COINGLASS_API_KEY = os.getenv("COINGLASS_API_KEY")

SYMBOL_CB   = "BTC-USD"
SYMBOL_FUT  = "BTC"   # used in Coinglass
START_DATE  = "2023-01-01"
END_DATE    = "2025-11-01"

OUT_CSV     = "output/fg2_daily.csv"
os.makedirs("output", exist_ok=True)


# ---------------- HELPER FUNCS ----------------

def iso_to_unix(date_str):
    dt = datetime.strptime(date_str, "%Y-%m-%d")
    return int(dt.timestamp())


def unix_to_date(ts):
    return datetime.utcfromtimestamp(ts).date()


def cb_get(path, params=None, sleep=0.25):
    """Coinbase public GET"""
    if params is None:
        params = {}
    url = COINBASE_BASE + path
    r = requests.get(url, params=params, timeout=30)
    if r.status_code != 200:
        raise RuntimeError(f"Coinbase error {r.status_code}: {r.text[:300]}")
    time.sleep(sleep)
    return r.json()


def coinglass_get(path, params=None, sleep=0.25):
    """Coinglass GET with API key"""
    if COINGLASS_API_KEY is None:
        raise RuntimeError("COINGLASS_API_KEY not set in environment.")
    if params is None:
        params = {}
    headers = {
        "accept": "application/json",
        "coinglassSecret": COINGLASS_API_KEY,
    }
    url = COINGLASS_BASE + path
    r = requests.get(url, params=params, headers=headers, timeout=30)
    if r.status_code != 200:
        raise RuntimeError(f"Coinglass error {r.status_code}: {r.text[:300]}")
    time.sleep(sleep)
    return r.json()


def cmc_get(path, params=None, sleep=0.25):
    """CoinMarketCap GET with API key"""
    if CMC_API_KEY is None:
        raise RuntimeError("CMC_API_KEY not set in environment.")
    if params is None:
        params = {}
    headers = {"X-CMC_PRO_API_KEY": CMC_API_KEY}
    url = CMC_BASE + path
    r = requests.get(url, params=params, headers=headers, timeout=30)
    if r.status_code != 200:
        raise RuntimeError(f"CMC error {r.status_code}: {r.text[:300]}")
    time.sleep(sleep)
    return r.json()


# ---------------- 1) COINBASE BTC SPOT (OHLCV) ----------------

def fetch_cb_btc_ohlcv(start_date, end_date):
    """
    Fetch daily BTC-USD candles from Coinbase.
    Coinbase /candles returns [time, low, high, open, close, volume]
    with granularity in seconds.
    """
    start_unix = iso_to_unix(start_date)
    end_unix   = iso_to_unix(end_date) + 24*60*60

    granularity = 86400  # 1 day in seconds
    params = {
        "granularity": granularity,
        "start": datetime.utcfromtimestamp(start_unix).isoformat() + "Z",
        "end": datetime.utcfromtimestamp(end_unix).isoformat() + "Z",
    }
    data = cb_get(f"/products/{SYMBOL_CB}/candles", params=params)

    # Coinbase returns newest first; reverse
    rows = []
    for row in data:
        ts, low, high, open_, close, volume = row
        date = unix_to_date(ts)
        rows.append((date, close, volume))

    df = pd.DataFrame(rows, columns=["date", "spot_close", "spot_volume"])
    df = df.drop_duplicates("date").sort_values("date")
    return df


# ---------------- 2) COINGLASS BTC OI & VOLUME ----------------

def fetch_coinglass_oi_and_volume(start_date, end_date):
    """
    Fetch BTC futures open interest & volume + spot volume from Coinglass.
    Exact field names may need slight tweaks based on your plan.
    We'll assume daily aggregated endpoints exist:
      - /futures/openInterestHistory
      - /futures/longShortHistory or /futures/futuresVolume
    """

    start_unix = iso_to_unix(start_date)
    end_unix   = iso_to_unix(end_date)

    # Open Interest history
    oi_js = coinglass_get(
        "/futures/openInterestHistory",
        params={
            "symbol": SYMBOL_FUT,
            "interval": "day",
            "startTime": start_unix,
            "endTime": end_unix,
        },
    )

    oi_rows = []
    # adjust "data" shape as needed
    for item in oi_js.get("data", []):
        ts = int(item["time"])
        date = unix_to_date(ts // 1000 if ts > 10**12 else ts)
        oi_value = float(item["openInterestUsd"])
        oi_rows.append((date, oi_value))

    oi_df = pd.DataFrame(oi_rows, columns=["date", "oi_usd"]).drop_duplicates("date")

    # Futures volume & spot volume
    vol_js = coinglass_get(
        "/futures/futuresVolume",
        params={
            "symbol": SYMBOL_FUT,
            "interval": "day",
            "startTime": start_unix,
            "endTime": end_unix,
        },
    )
    v_rows = []
    for item in vol_js.get("data", []):
        ts = int(item["time"])
        date = unix_to_date(ts // 1000 if ts > 10**12 else ts)
        perp_vol = float(item["futuresVolumeUsd"])
        spot_vol = float(item.get("spotVolumeUsd", 0.0))  # if endpoint provides it
        v_rows.append((date, perp_vol, spot_vol))

    vol_df = pd.DataFrame(v_rows, columns=["date", "perp_volume_usd", "cg_spot_volume_usd"])
    vol_df = vol_df.drop_duplicates("date")

    df = oi_df.merge(vol_df, on="date", how="inner")
    df = df.sort_values("date")
    return df


# ---------------- 3) COINMARKETCAP GLOBAL CAPS ----------------

def fetch_cmc_global_daily(start_date, end_date):
    """
    Fetch daily global market metrics from CoinMarketCap between start_date and end_date.
    Uses global-metrics/quotes/historical.
    """
    js = cmc_get(
        "/v1/global-metrics/quotes/historical",
        params={
            "time_start": start_date,
            "time_end": end_date,
            "interval": "1d",
        },
    )

    rows = []
    for item in js.get("data", {}).get("quotes", []):
        ts_str = item["timestamp"][:10]
        date = datetime.strptime(ts_str, "%Y-%m-%d").date()
        quote = item["quote"]["USD"]
        total_mc  = float(quote["total_market_cap"])
        # adjust key if needed based on your plan:
        stable_mc = float(quote.get("stablecoin_market_cap", 0.0))
        rows.append((date, total_mc, stable_mc))

    df = pd.DataFrame(rows, columns=["date", "total_mc_usd", "stable_mc_usd"])
    df = df.sort_values("date")
    return df


# ---------------- UTILITIES ----------------

def clip01(x):
    return np.minimum(1, np.maximum(0, x))


def rolling_minmax(series, window=365, lower_q=0.05, upper_q=0.95):
    low = series.rolling(window).quantile(lower_q)
    high = series.rolling(window).quantile(upper_q)
    return low, high


# ---------------- COMPUTE FG2 ----------------

def compute_fg2(df):
    """
    df has columns:
      spot_close, spot_volume,
      perp_volume_usd, cg_spot_volume_usd,
      oi_usd, total_mc_usd, stable_mc_usd
    """
    eps = 1e-9

    # 1) Volatility: realized 30d and 90d
    df["log_ret"] = np.log(df["spot_close"] / df["spot_close"].shift(1))
    df["RV_30"] = df["log_ret"].rolling(30).std() * np.sqrt(365)
    df["RV_90"] = df["log_ret"].rolling(90).std() * np.sqrt(365)

    V_raw = df["RV_90"] / (df["RV_30"] + eps)  # calm > 1
    V_low, V_high = rolling_minmax(V_raw)
    V_score = 100 * clip01((V_raw - V_low) / (V_high - V_low + eps))

    # 2) Open Interest ratio: OI / total_mc
    OI_ratio = df["oi_usd"] / (df["total_mc_usd"] + eps)
    OI_low, OI_high = rolling_minmax(OI_ratio)
    OI_score = 100 * clip01((OI_ratio - OI_low) / (OI_high - OI_low + eps))

    # 3) Spot vs Perp: perp fraction (using Coinglass volumes)
    df["perp_frac"] = df["perp_volume_usd"] / (
        df["perp_volume_usd"] + df["cg_spot_volume_usd"] + eps
    )
    PF_low, PF_high = rolling_minmax(df["perp_frac"])
    SP_score = 100 * clip01((df["perp_frac"] - PF_low) / (PF_high - PF_low + eps))

    # 4) Risk Deployment: RiskyMC = total - stable
    df["risky_mc"] = df["total_mc_usd"] - df["stable_mc_usd"]
    df["d_risky"] = df["risky_mc"].diff()
    df["risk_flow"] = df["d_risky"].ewm(span=7, adjust=False).mean()
    RF_low, RF_high = rolling_minmax(df["risk_flow"])
    RF_score = 100 * clip01((df["risk_flow"] - RF_low) / (RF_high - RF_low + eps))

    # Composite FG2
    FG2 = (
        0.40 * RF_score +
        0.30 * OI_score +
        0.20 * SP_score +
        0.10 * V_score
    )

    out = df.copy()
    out["FG2"]          = FG2
    out["FG2_vol"]      = V_score
    out["FG2_oi"]       = OI_score
    out["FG2_spotperp"] = SP_score
    out["FG2_riskflow"] = RF_score
    return out


# ---------------- MAIN ----------------

def main():
    print("Fetching Coinbase BTC-USD OHLCV…")
    cb_df = fetch_cb_btc_ohlcv(START_DATE, END_DATE)

    print("Fetching Coinglass BTC OI & volumes…")
    cg_df = fetch_coinglass_oi_and_volume(START_DATE, END_DATE)

    print("Fetching CMC global metrics…")
    cmc_df = fetch_cmc_global_daily(START_DATE, END_DATE)

    # Merge everything on date
    df = cb_df.merge(cg_df, on="date", how="inner")
    df = df.merge(cmc_df, on="date", how="inner")

    df = df.sort_values("date")
    print("Computing FG2 components…")
    fg2_df = compute_fg2(df)

    fg2_df.to_csv(OUT_CSV, index=False)
    print(f"Saved FG2 daily data to {OUT_CSV}")
    print(fg2_df[["date", "FG2", "FG2_vol", "FG2_oi", "FG2_spotperp", "FG2_riskflow"]].tail())


if __name__ == "__main__":
    main()
        
