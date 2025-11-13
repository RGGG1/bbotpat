"""
Compute Fear & Greed 2.0 index (FG2) using:

- Coinbase: BTC-USD daily OHLCV (for price & spot volume)
- Coinalyze: BTC perp open interest history + perp OHLCV (for perp volume)
- CoinGecko: historical market caps for a small basket of coins
    - Risk assets:  bitcoin, ethereum, solana, binancecoin, ripple
    - Stables:      tether, usd-coin

Risk Deployment proxy:
    RISK_MC  = sum(MC of risk_ids)
    STABLE_MC = sum(MC of stable_ids)
    rd_ratio = RISK_MC / (RISK_MC + STABLE_MC)
    RD_score = 100 * rd_ratio

FG2 = 0.40 * RD_score
     + 0.30 * OI_score
     + 0.20 * SP_score
     + 0.10 * V_score

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
COINALYZE_BASE  = "https://api.coinalyze.net/v1"
COINGECKO_BASE  = "https://api.coingecko.com/api/v3"

COINALYZE_API_KEY = os.getenv("COINALYZE_API_KEY")

SYMBOL_CB    = "BTC-USD"          # Coinbase product
SYMBOL_PERP  = "BTCUSDT_PERP.A"   # Coinalyze aggregated BTC perp symbol

START_DATE   = "2023-01-01"
END_DATE     = "2025-11-01"

# Risk & stable baskets for Risk Deployment proxy
RISK_IDS   = ["bitcoin", "ethereum", "solana", "binancecoin", "ripple"]
STABLE_IDS = ["tether", "usd-coin"]

OUT_CSV    = "output/fg2_daily.csv"
os.makedirs("output", exist_ok=True)


# ---------------- HELPER FUNCS ----------------

def iso_to_unix(date_str: str) -> int:
    dt = datetime.strptime(date_str, "%Y-%m-%d")
    return int(dt.timestamp())


def unix_to_date(ts: int):
    return datetime.utcfromtimestamp(ts).date()


def cb_get(path, params=None, sleep=0.25):
    """Coinbase public GET."""
    if params is None:
        params = {}
    url = COINBASE_BASE + path
    r = requests.get(url, params=params, timeout=30)
    if r.status_code != 200:
        raise RuntimeError(f"Coinbase error {r.status_code}: {r.text[:300]}")
    time.sleep(sleep)
    return r.json()


def coinalyze_get(path, params=None, sleep=0.25):
    """Coinalyze GET with API key."""
    if COINALYZE_API_KEY is None:
        raise RuntimeError("COINALYZE_API_KEY not set in environment.")
    if params is None:
        params = {}
    headers = { "accept": "application/json" }
    params = { **params, "api_key": COINALYZE_API_KEY }
    url = COINALYZE_BASE + path
    r = requests.get(url, params=params, headers=headers, timeout=30)
    if r.status_code != 200:
        raise RuntimeError(f"Coinalyze error {r.status_code}: {r.text[:300]}")
    time.sleep(sleep)
    return r.json()


def cg_get(path, params=None, sleep=1.2):
    """CoinGecko public GET (no key)."""
    if params is None:
        params = {}
    url = COINGECKO_BASE + path
    r = requests.get(url, params=params, timeout=60)
    if r.status_code != 200:
        raise RuntimeError(f"CoinGecko error {r.status_code}: {r.text[:300]}")
    time.sleep(sleep)
    return r.json()


# ---------------- 1) COINBASE BTC SPOT (OHLCV) ----------------

def fetch_cb_btc_ohlcv(start_date, end_date):
    """
    Fetch daily BTC-USD candles from Coinbase in <=300-candle chunks.

    Coinbase /candles returns [time, low, high, open, close, volume]
    with granularity in seconds.
    """
    granularity = 86400
    max_points = 300

    start_unix = iso_to_unix(start_date)
    end_unix   = iso_to_unix(end_date) + 24 * 60 * 60

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

        oldest_ts = min(r[0] for r in data)
        cur_start = oldest_ts + granularity
        if cur_start <= start_unix:
            break
        start_unix = cur_start

    df = pd.DataFrame(rows, columns=["date", "spot_close", "spot_volume"])
    return df.drop_duplicates("date").sort_values("date")


# ---------------- 2) COINALYZE BTC PERP OI & VOLUME ----------------

def fetch_coinalyze_oi_and_perp_volume(start_date, end_date):
    start_unix = iso_to_unix(start_date)
    end_unix   = iso_to_unix(end_date)

    # OI history
    oi_js = coinalyze_get(
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
    for item in oi_js:
        if item.get("symbol") != SYMBOL_PERP:
            continue
        for h in item.get("history", []):
            ts = int(h["t"])
            oi_rows.append((unix_to_date(ts), float(h["c"])))

    oi_df = pd.DataFrame(oi_rows, columns=["date", "oi_usd"])

    # Perp OHLCV (volume)
    ohlcv_js = coinalyze_get(
        "/ohlcv-history",
        params={
            "symbols": SYMBOL_PERP,
            "interval": "daily",
            "from": start_unix,
            "to": end_unix,
        },
    )

    vol_rows = []
    for item in ohlcv_js:
        if item.get("symbol") != SYMBOL_PERP:
            continue
        for h in item.get("history", []):
            ts = int(h["t"])
            vol_rows.append((unix_to_date(ts), float(h["v"])))

    vol_df = pd.DataFrame(vol_rows, columns=["date", "perp_volume"])

    df = oi_df.merge(vol_df, on="date", how="inner")
    return df.sort_values("date")


# ---------------- 3) COINGECKO RISK DEPLOYMENT PROXY ----------------

def fetch_cg_risk_deployment(start_date, end_date):
    """
    Fetch historical market caps for RISK_IDS and STABLE_IDS from CoinGecko,
    using only 365 days of data (free plan limit).
    """
    start_dt = datetime.strptime(start_date, "%Y-%m-%d").date()
    end_dt   = datetime.strptime(end_date, "%Y-%m-%d").date()

    all_ids = RISK_IDS + STABLE_IDS
    frames = []

    for cid in all_ids:
        js = cg_get(
            f"/coins/{cid}/market_chart",
            params={"vs_currency": "usd", "days": "365"}   # <— FIXED HERE
        )
        rows = []
        for ts, cap in js.get("market_caps", []):
            date = datetime.utcfromtimestamp(ts / 1000.0).date()
            if start_dt <= date <= end_dt:
                rows.append((date, cap))

        df = pd.DataFrame(rows, columns=["date", f"{cid}_mc"])
        frames.append(df)

    if not frames:
        raise RuntimeError("No CoinGecko data fetched for risk deployment.")

    df = frames[0]
    for f in frames[1:]:
        df = df.merge(f, on="date", how="outer")

    df = df.sort_values("date").drop_duplicates("date")
    df.set_index("date", inplace=True)
    df = df.ffill()

    risk_cols = [f"{cid}_mc" for cid in RISK_IDS]
    stable_cols = [f"{cid}_mc" for cid in STABLE_IDS]

    eps = 1e-9
    df["risk_mc"]   = df[risk_cols].sum(axis=1)
    df["stable_mc"] = df[stable_cols].sum(axis=1)
    df["rd_ratio"]  = df["risk_mc"] / (df["risk_mc"] + df["stable_mc"] + eps)
    df["RD_score"]  = 100 * df["rd_ratio"]

    df = df.reset_index()
    return df[["date", "RD_score", "risk_mc", "stable_mc"]]


# ---------------- UTILITIES ----------------

def clip01(x):
    return np.minimum(1, np.maximum(0, x))


def rolling_minmax(series, window=365, lower_q=0.05, upper_q=0.95):
    low = series.rolling(window).quantile(lower_q)
    high = series.rolling(window).quantile(upper_q)
    return low, high


# ---------------- COMPUTE FG2 ----------------

def compute_fg2(df):
    eps = 1e-9

    # Volatility scores
    df["log_ret"] = np.log(df["spot_close"] / df["spot_close"].shift(1))
    df["RV_30"] = df["log_ret"].rolling(30).std() * np.sqrt(365)
    df["RV_90"] = df["log_ret"].rolling(90).std() * np.sqrt(365)

    V_raw = df["RV_90"] / (df["RV_30"] + eps)
    V_low, V_high = rolling_minmax(V_raw)
    V_score = 100 * clip01((V_raw - V_low) / (V_high - V_low + eps))

    # OI score
    OI = df["oi_usd"]
    OI_low, OI_high = rolling_minmax(OI)
    OI_score = 100 * clip01((OI - OI_low) / (OI_high - OI_low + eps))

    # Spot/perp score
    df["perp_frac"] = df["perp_volume"] / (df["perp_volume"] + df["spot_volume"] + eps)
    PF_low, PF_high = rolling_minmax(df["perp_frac"])
    SP_score = 100 * clip01((df["perp_frac"] - PF_low) / (PF_high - PF_low + eps))

    RD_score = df["RD_score"]

    FG2 = (
        0.40 * RD_score +
        0.30 * OI_score +
        0.20 * SP_score +
        0.10 * V_score
    )

    out = df.copy()
    out["FG2"] = FG2
    out["FG2_vol"] = V_score
    out["FG2_oi"]  = OI_score
    out["FG2_spotperp"] = SP_score
    out["FG2_riskdep"]  = RD_score

    return out


# ---------------- MAIN ----------------

def main():
    print("Fetching Coinbase BTC-USD OHLCV…")
    cb_df = fetch_cb_btc_ohlcv(START_DATE, END_DATE)

    print("Fetching Coinalyze BTC perp OI & volumes…")
    cl_df = fetch_coinalyze_oi_and_perp_volume(START_DATE, END_DATE)

    print("Fetching CoinGecko risk deployment proxy…")
    cg_df = fetch_cg_risk_deployment(START_DATE, END_DATE)

    print("Merging datasets…")
    df = cb_df.merge(cl_df, on="date", how="inner")
    df = df.merge(cg_df, on="date", how="inner")
    df = df.sort_values("date")

    print("Computing FG2 components…")
    fg2_df = compute_fg2(df)

    fg2_df.to_csv(OUT_CSV, index=False)
    print(f"Saved FG2 daily data to {OUT_CSV}")
    print(fg2_df.tail())


if __name__ == "__main__":
    main()
