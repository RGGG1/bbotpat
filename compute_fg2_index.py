"""
Compute Fear & Greed 2.0 index (FG2) using:

- Coinbase: BTC-USD daily OHLCV (for price & spot volume)
- Coinalyze: BTC perp open interest history + perp OHLCV (for perp volume)
- CoinPaprika: historical market caps for a small basket of coins
    - Risk assets:  bitcoin, ethereum, solana, bnb, xrp
    - Stables:      tether, usdc

Risk Deployment proxy:
    RISK_MC   = sum(MC of risk_ids)
    STABLE_MC = sum(MC of stable_ids)
    rd_ratio  = RISK_MC / (RISK_MC + STABLE_MC)
    RD_score  = 100 * rd_ratio

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

COINBASE_BASE    = "https://api.exchange.coinbase.com"
COINALYZE_BASE   = "https://api.coinalyze.net/v1"
COINPAPRIKA_BASE = "https://api.coinpaprika.com/v1"

COINALYZE_API_KEY = os.getenv("COINALYZE_API_KEY")

SYMBOL_CB    = "BTC-USD"          # Coinbase product
SYMBOL_PERP  = "BTCUSDT_PERP.A"   # Coinalyze aggregated BTC perp symbol

START_DATE   = "2023-01-01"
END_DATE     = "2025-11-01"

# CoinPaprika coin IDs for risk & stables
RISK_IDS = [
    "btc-bitcoin",
    "eth-ethereum",
    "sol-solana",
    "bnb-bnb",
    "xrp-xrp",
]
STABLE_IDS = [
    "usdt-tether",
    "usdc-usdc",
]

OUT_CSV = "output/fg2_daily.csv"
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
    headers = {"accept": "application/json"}
    params = {**params, "api_key": COINALYZE_API_KEY}
    url = COINALYZE_BASE + path
    r = requests.get(url, params=params, headers=headers, timeout=30)
    if r.status_code != 200:
        raise RuntimeError(f"Coinalyze error {r.status_code}: {r.text[:300]}")
    time.sleep(sleep)
    return r.json()


def cp_get(path, params=None, sleep=0.4):
    """CoinPaprika public GET (no key, free plan)."""
    if params is None:
        params = {}
    url = COINPAPRIKA_BASE + path
    r = requests.get(url, params=params, timeout=60)
    if r.status_code != 200:
        raise RuntimeError(f"CoinPaprika error {r.status_code}: {r.text[:300]}")
    time.sleep(sleep)  # polite rate limiting
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


# ---------------- 3) COINPAPRIKA RISK DEPLOYMENT PROXY ----------------

def fetch_cp_risk_deployment(start_date, end_date):
    """
    Fetch historical market caps for RISK_IDS and STABLE_IDS from CoinPaprika.

    Free plan: daily historical data is only available for approx. the last 365 days
    counted from *today*, not from END_DATE.

    So we clamp the effective start date to max(START_DATE, today - 365 days).
    """
    start_dt = datetime.strptime(start_date, "%Y-%m-%d").date()
    end_dt   = datetime.strptime(end_date, "%Y-%m-%d").date()

    today = datetime.utcnow().date()
    min_start_allowed = today - timedelta(days=365)

    effective_start = max(start_dt, min_start_allowed)

    # Ensure end_dt is not before effective_start
    if end_dt < effective_start:
        raise RuntimeError(
            f"END_DATE {end_dt} is older than free historical window "
            f"({effective_start} onward) for CoinPaprika."
        )

    all_ids = RISK_IDS + STABLE_IDS
    frames = []

    for cid in all_ids:
        js = cp_get(
            f"/tickers/{cid}/historical",
            params={
                "start": effective_start.isoformat(),
                "end": end_dt.isoformat(),
                "interval": "1d",
            }
        )
        rows = []
        for entry in js:
            ts = entry.get("timestamp")
            mc = entry.get("market_cap")
            if ts is None or mc is None:
                continue
            date = datetime.fromisoformat(ts.replace("Z", "+00:00")).date()
            if effective_start <= date <= end_dt:
                rows.append((date, float(mc)))
        df = pd.DataFrame(rows, columns=["date", f"{cid}_mc"])
        frames.append(df)

    if not frames:
        raise RuntimeError("No CoinPaprika data fetched for risk deployment.")

    df = frames[0]
    for f in frames[1:]:
        df = df.merge(f, on="date", how="outer")

    df = df.sort_values("date").drop_duplicates("date")
    df.set_index("date", inplace=True)
    df = df.ffill()

    risk_cols   = [f"{cid}_mc" for cid in RISK_IDS]
    stable_cols = [f"{cid}_mc" for cid in STABLE_IDS]

    eps = 1e-9
    df["risk_mc"]   = df[risk_cols].sum(axis=1)
    df["stable_mc"] = df[stable_cols].sum(axis=1)
    df["rd_ratio"]  = df["risk_mc"] / (df["risk_mc"] + df["stable_mc"] + eps)
    df["RD_score"]  = 100.0 * df["rd_ratio"]

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
    out["FG2"]          = FG2
    out["FG2_vol"]      = V_score
    out["FG2_oi"]       = OI_score
    out["FG2_spotperp"] = SP_score
    out["FG2_riskdep"]  = RD_score

    return out


# ---------------- MAIN ----------------

def main():
    print("Fetching Coinbase BTC-USD OHLCV…")
    cb_df = fetch_cb_btc_ohlcv(START_DATE, END_DATE)

    print("Fetching Coinalyze BTC perp OI & volumes…")
    cl_df = fetch_coinalyze_oi_and_perp_volume(START_DATE, END_DATE)

    print("Fetching CoinPaprika risk deployment proxy…")
    cp_df = fetch_cp_risk_deployment(START_DATE, END_DATE)

    print("Merging datasets…")
    df = cb_df.merge(cl_df, on="date", how="inner")
    df = df.merge(cp_df, on="date", how="inner")
    df = df.sort_values("date")

    print("Computing FG2 components…")
    fg2_df = compute_fg2(df)

    fg2_df.to_csv(OUT_CSV, index=False)
    print(f"Saved FG2 daily data to {OUT_CSV}")
    print(fg2_df.tail())


if __name__ == "__main__":
    main()
    
