"""
Compute Hive Mind Index (HMI) — Binance-only, using backfilled history.

Pipeline:

1) backfill_hmi_history.py (run once) creates:
   data/hmi_oi_history.csv with columns:
       date, spot_close, spot_volume, perp_volume, oi_usd

2) This script (compute_fg2_index.py) for daily / twice-daily runs:
   - Load data/hmi_oi_history.csv
   - Fetch latest BTCUSDT spot daily kline from Binance
   - Fetch latest BTCUSDT perps daily kline + open interest from Binance
   - Update/append today's row
   - Trim history to last 730 days
   - Compute HMI (FG_lite) using 365d rolling quantiles
   - Write:
       output/fg2_daily.csv
       hmi_latest.json
       docs/hmi_latest.json
"""

import json
from pathlib import Path
from datetime import datetime, timedelta

import requests
import pandas as pd
import numpy as np

BINANCE_SPOT_BASE = "https://api.binance.com"
BINANCE_FUTURES_BASE = "https://fapi.binance.com"

SPOT_SYMBOL = "BTCUSDT"
DATA_CSV = Path("data/hmi_oi_history.csv")

OUT_DIR = Path("output")
OUT_DIR.mkdir(exist_ok=True)
OUT_CSV = OUT_DIR / "fg2_daily.csv"

HMI_JSON_ROOT = Path("hmi_latest.json")
HMI_JSON_DOCS = Path("docs/hmi_latest.json")
Path("docs").mkdir(exist_ok=True)


# ---------- BINANCE HELPERS ----------

def bn_spot_get_klines(symbol: str, interval: str, limit: int = 1):
    url = BINANCE_SPOT_BASE + "/api/v3/klines"
    params = {"symbol": symbol, "interval": interval, "limit": limit}
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


def fetch_today_spot_and_perps():
    """
    Fetch today's BTCUSDT daily spot candle and perps daily candle + OI.

    Returns:
        (date, spot_close, spot_quote_volume, perp_quote_volume, oi_usd)
    """
    # Spot daily kline
    spot_kl = bn_spot_get_klines(SPOT_SYMBOL, "1d", limit=1)
    if not spot_kl:
        raise RuntimeError("No BTCUSDT spot kline returned.")
    sk = spot_kl[-1]
    # [open_time, open, high, low, close, volume, close_time, quote_volume, ...]
    open_time_ms = sk[0]
    d = datetime.utcfromtimestamp(open_time_ms / 1000.0).date()
    spot_close = float(sk[4])
    spot_quote_vol = float(sk[7])

    # Perps daily kline
    perp_kl = bn_futures_get("/fapi/v1/klines", params={
        "symbol": SPOT_SYMBOL,
        "interval": "1d",
        "limit": 1,
    })
    if not perp_kl:
        raise RuntimeError("No BTCUSDT perps kline returned.")
    pk = perp_kl[-1]
    perp_quote_vol = float(pk[7])

    # Current open interest (contracts)
    oi_js = bn_futures_get("/fapi/v1/openInterest", params={"symbol": SPOT_SYMBOL})
    oi_contracts = float(oi_js["openInterest"])
    oi_usd = oi_contracts * spot_close

    return d, spot_close, spot_quote_vol, perp_quote_vol, oi_usd


# ---------- HMI SCORING ----------

def clip01(x):
    return np.minimum(1, np.maximum(0, x))


def rolling_minmax(series: pd.Series, window: int = 365, lower_q: float = 0.05, upper_q: float = 0.95):
    """
    Rolling quantile low/high with a fixed window, requiring full window.
    """
    eps = 1e-9
    low = series.rolling(window=window, min_periods=window).quantile(lower_q)
    high = series.rolling(window=window, min_periods=window).quantile(upper_q)
    # Avoid degenerate ranges
    mask = (high - low).abs() < eps
    high[mask] = low[mask] + eps
    return low, high


def compute_fg_lite(df: pd.DataFrame):
    """
    Compute FG_lite and its components from:

        spot_close, spot_volume, perp_volume, oi_usd

    Returns a copy of df with FG_lite, FG_vol, FG_oi, FG_spotperp.
    """
    df = df.sort_values("date").reset_index(drop=True).copy()
    eps = 1e-9

    # volatility: log returns of spot_close
    df["log_ret"] = np.log(df["spot_close"] / df["spot_close"].shift(1))
    df["RV_30"] = df["log_ret"].rolling(30).std() * np.sqrt(365)
    df["RV_90"] = df["log_ret"].rolling(90).std() * np.sqrt(365)

    V_raw = df["RV_90"] / (df["RV_30"] + eps)
    V_low, V_high = rolling_minmax(V_raw, window=365)
    V_score = 100 * clip01((V_raw - V_low) / (V_high - V_low + eps))

    # open interest (oi_usd)
    OI = df["oi_usd"]
    OI_low, OI_high = rolling_minmax(OI, window=365)
    OI_score = 100 * clip01((OI - OI_low) / (OI_high - OI_low + eps))

    # spot vs perp dominance
    df["perp_frac"] = df["perp_volume"] / (df["perp_volume"] + df["spot_volume"] + eps)
    PF_low, PF_high = rolling_minmax(df["perp_frac"], window=365)
    SP_score = 100 * clip01((df["perp_frac"] - PF_low) / (PF_high - PF_low + eps))

    FG_lite = (
        0.50 * OI_score +
        0.30 * SP_score +
        0.20 * V_score
    )

    out = df.copy()
    out["FG_lite"] = FG_lite
    out["FG_vol"] = V_score
    out["FG_oi"] = OI_score
    out["FG_spotperp"] = SP_score
    return out


def hmi_band_label(hmi: float) -> str:
    if hmi < 10:
        return "Zombie apocalypse"
    if hmi < 25:
        return "McDonald's applications"
    if hmi < 40:
        return "Ngmi"
    if hmi < 60:
        return "Stable"
    if hmi < 80:
        return "We're early"
    return "It's the future of finance"


# ---------- MAIN PIPELINE ----------

def load_history():
    if not DATA_CSV.exists():
        raise RuntimeError(
            f"{DATA_CSV} not found. Run backfill_hmi_history.py once to create it."
        )

    df = pd.read_csv(DATA_CSV, parse_dates=["date"])
    if df.empty:
        raise RuntimeError(f"{DATA_CSV} is empty.")

    df["date"] = df["date"].dt.date
    df = df.sort_values("date")
    return df


def update_today_row(df: pd.DataFrame) -> pd.DataFrame:
    today, spot_close, spot_vol, perp_vol, oi_usd = fetch_today_spot_and_perps()

    row = {
        "date": today,
        "spot_close": spot_close,
        "spot_volume": spot_vol,
        "perp_volume": perp_vol,
        "oi_usd": oi_usd,
    }

    df = df.copy()
    if today in df["date"].values:
        df.loc[df["date"] == today, ["spot_close", "spot_volume", "perp_volume", "oi_usd"]] = [
            spot_close,
            spot_vol,
            perp_vol,
            oi_usd,
        ]
    else:
        df = pd.concat([df, pd.DataFrame([row])], ignore_index=True)

    # Trim to last 730 days
    df["date"] = df["date"].astype("datetime64[ns]").dt.date
    df = df.sort_values("date")
    cutoff = datetime.utcnow().date() - timedelta(days=730)
    df = df[df["date"] >= cutoff]

    return df


def write_history(df: pd.DataFrame):
    df_out = df.copy()
    df_out["date"] = pd.to_datetime(df_out["date"])
    df_out.to_csv(DATA_CSV, index=False)
    print(f"Saved updated history to {DATA_CSV}")


def write_hmi_json(fg_df: pd.DataFrame):
    # Require at least 365 days of non-NaN FG_lite
    valid = fg_df.dropna(subset=["FG_lite"]).copy()
    if valid.empty or len(valid) < 365:
        raise RuntimeError(
            f"Not enough valid FG_lite history ({len(valid)} rows) to compute HMI (need >=365)."
        )

    last_row = valid.iloc[-1]
    hmi_val = float(last_row["FG_lite"])
    date_val = last_row["date"]

    band = hmi_band_label(hmi_val)

    payload = {
        "hmi": round(hmi_val, 1),
        "band": band,
        "date": str(date_val),
    }

    HMI_JSON_ROOT.write_text(json.dumps(payload, indent=2))
    HMI_JSON_DOCS.write_text(json.dumps(payload, indent=2))
    print(f"Wrote HMI JSONs: {HMI_JSON_ROOT}, {HMI_JSON_DOCS} (HMI={payload['hmi']})")


def main():
    print("Loading HMI history…")
    df = load_history()
    print(f"History rows before update: {len(df)}")

    df = update_today_row(df)
    print(f"History rows after update: {len(df)}")

    write_history(df)

    print("Computing HMI (FG_lite)…")
    fg_df = compute_fg_lite(df)
    fg_df["date"] = fg_df["date"].astype("datetime64[ns]").dt.date

    OUT_CSV.parent.mkdir(exist_ok=True)
    fg_df.to_csv(OUT_CSV, index=False)
    print(f"Saved HMI data to {OUT_CSV}")
    print(fg_df[["date", "FG_lite", "FG_vol", "FG_oi", "FG_spotperp"]].tail())

    write_hmi_json(fg_df)


if __name__ == "__main__":
    main()
