"""
Compute Fear & Greed 2.0 index (FG2) using:

- Binance spot: BTCUSDT klines (for price & spot volume)
- Binance futures: BTCUSDT perps (for perp volume & open interest)
- CoinMarketCap: global metrics (for total vs stablecoin market cap)

Outputs a CSV with daily FG2 and component scores.
"""

import os
import time
from datetime import datetime, timedelta

import requests
import pandas as pd
import numpy as np

# ---------------- CONFIG ----------------

BINANCE_SPOT_BASE   = "https://api.binance.com"
BINANCE_FUTURES_BASE = "https://fapi.binance.com"
CMC_BASE            = "https://pro-api.coinmarketcap.com"

CMC_API_KEY = os.getenv("CMC_API_KEY")  # you must set this in GitHub secrets or env

SYMBOL         = "BTCUSDT"
START_DATE     = "2023-01-01"
END_DATE       = "2025-11-01"

OUT_CSV        = "output/fg2_daily.csv"
os.makedirs("output", exist_ok=True)


# ---------------- HELPER FUNCS ----------------

def iso_to_millis(date_str):
    dt = datetime.strptime(date_str, "%Y-%m-%d")
    return int(dt.timestamp() * 1000)


def millis_to_date(ms):
    return datetime.utcfromtimestamp(ms / 1000.0).date()


def binance_get(base, path, params=None, sleep=0.3):
    if params is None:
        params = {}
    url = base + path
    r = requests.get(url, params=params, timeout=30)
    if r.status_code != 200:
        raise RuntimeError(f"Binance error {r.status_code}: {r.text[:300]}")
    time.sleep(sleep)
    return r.json()


def cmc_get(path, params=None, sleep=0.5):
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


# ---------------- 1) BINANCE SPOT KLINES (price + spot vol) ----------------

def fetch_spot_klines(symbol, interval, start_date, end_date):
    """
    Fetch daily klines for symbol from Binance spot.
    Returns DataFrame with date, close, spot_quote_volume.
    """
    start_ms = iso_to_millis(start_date)
    end_ms   = iso_to_millis(end_date) + 24*60*60*1000  # include end date

    all_rows = []
    cur_start = start_ms
    while cur_start < end_ms:
        data = binance_get(
            BINANCE_SPOT_BASE,
            "/api/v3/klines",
            params={
                "symbol": symbol,
                "interval": interval,
                "startTime": cur_start,
                "endTime": end_ms,
                "limit": 1000,
            },
        )
        if not data:
            break
        for row in data:
            open_time = row[0]
            close = float(row[4])
            quote_volume = float(row[7])  # quote asset volume
            date = millis_to_date(open_time)
            all_rows.append((date, close, quote_volume))
        if len(data) < 1000:
            break
        cur_start = data[-1][0] + 1

    df = pd.DataFrame(all_rows, columns=["date", "spot_close", "spot_quote_volume"])
    df = df.drop_duplicates("date").sort_values("date")
    return df


# ---------------- 2) BINANCE FUTURES KLINES (perp vol) ----------------

def fetch_futures_klines(symbol, interval, start_date, end_date):
    """
    Fetch daily futures klines for symbol from Binance USDT-margined futures.
    Returns DataFrame with date, perp_quote_volume.
    """
    start_ms = iso_to_millis(start_date)
    end_ms   = iso_to_millis(end_date) + 24*60*60*1000

    all_rows = []
    cur_start = start_ms
    while cur_start < end_ms:
        data = binance_get(
            BINANCE_FUTURES_BASE,
            "/fapi/v1/klines",
            params={
                "symbol": symbol,
                "interval": interval,
                "startTime": cur_start,
                "endTime": end_ms,
                "limit": 1500,
            },
        )
        if not data:
            break
        for row in data:
            open_time = row[0]
            quote_volume = float(row[7])  # quote asset volume
            date = millis_to_date(open_time)
            all_rows.append((date, quote_volume))
        if len(data) < 1500:
            break
        cur_start = data[-1][0] + 1

    df = pd.DataFrame(all_rows, columns=["date", "perp_quote_volume"])
    df = df.drop_duplicates("date").sort_values("date")
    return df


# ---------------- 3) BINANCE FUTURES OPEN INTEREST ----------------

def fetch_futures_oi(symbol, start_date, end_date):
    """
    Fetch daily open interest history for symbol from Binance futures.
    Uses openInterestHist endpoint with period=1d.
    """
    start_ts = datetime.strptime(start_date, "%Y-%m-%d")
    end_ts   = datetime.strptime(end_date, "%Y-%m-%d")

    all_rows = []
    cur_start = start_ts
    while cur_start <= end_ts:
        next_end = min(cur_start + timedelta(days=500), end_ts)
        data = binance_get(
            BINANCE_FUTURES_BASE,
            "/futures/data/openInterestHist",
            params={
                "symbol": symbol,
                "period": "1d",
                "limit": 500,
                "startTime": int(cur_start.timestamp() * 1000),
                "endTime": int(next_end.timestamp() * 1000),
            },
        )
        if not data:
            break
        for row in data:
            ts = int(row["timestamp"])
            oi = float(row["sumOpenInterestValue"])
            date = millis_to_date(ts)
            all_rows.append((date, oi))
        cur_start = next_end + timedelta(days=1)

    df = pd.DataFrame(all_rows, columns=["date", "oi_usd"])
    df = df.drop_duplicates("date").sort_values("date")
    return df


# ---------------- 4) COINMARKETCAP GLOBAL METRICS ----------------

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
        total_mc    = float(quote["total_market_cap"])
        # field name for stablecoin mc may differ; adjust if needed:
        stable_mc   = float(quote.get("stablecoin_market_cap", 0.0))
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
      spot_close, spot_quote_volume, perp_quote_volume,
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

    # 2) Open Interest ratio: OI / total_mc (or BTC mc if you pull it separately)
    # here we normalize by total_mc as a proxy:
    OI_ratio = df["oi_usd"] / (df["total_mc_usd"] + eps)
    OI_low, OI_high = rolling_minmax(OI_ratio)
    OI_score = 100 * clip01((OI_ratio - OI_low) / (OI_high - OI_low + eps))

    # 3) Spot vs Perp: perp fraction
    df["perp_frac"] = df["perp_quote_volume"] / (
        df["perp_quote_volume"] + df["spot_quote_volume"] + eps
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
    print("Fetching Binance spot klines…")
    spot_df = fetch_spot_klines(SYMBOL, "1d", START_DATE, END_DATE)

    print("Fetching Binance perp klines…")
    fut_df = fetch_futures_klines(SYMBOL, "1d", START_DATE, END_DATE)

    print("Fetching Binance futures OI…")
    oi_df = fetch_futures_oi(SYMBOL, START_DATE, END_DATE)

    print("Fetching CMC global metrics…")
    cmc_df = fetch_cmc_global_daily(START_DATE, END_DATE)

    # Merge everything on date
    df = spot_df.merge(fut_df, on="date", how="inner")
    df = df.merge(oi_df, on="date", how="inner")
    df = df.merge(cmc_df, on="date", how="inner")

    df = df.sort_values("date")
    print("Computing FG2 components…")
    fg2_df = compute_fg2(df)

    fg2_df.to_csv(OUT_CSV, index=False)
    print(f"Saved FG2 daily data to {OUT_CSV}")
    print(fg2_df[["date", "FG2", "FG2_vol", "FG2_oi", "FG2_spotperp", "FG2_riskflow"]].tail())


if __name__ == "__main__":
    main()
          
