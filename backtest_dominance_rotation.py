#!/usr/bin/env python3
"""
BTC dominance rotation backtest with FG_lite integration.

- BTC vs ALT bucket (ETH+SOL+BNB)
- Uses CoinGecko for daily prices/market caps (last ~365d)
- Uses FG_lite from output/fg2_daily.csv
- Allocation logic:

  1) If BTC dominance in [0.771, 0.789] -> 100% stables
  2) Else if FG_lite >= 77 -> 100% stables
  3) Else (Fear+Neutral) -> pure dominance rotation:

        dom <= 0.75 -> 100% BTC
        dom >= 0.81 -> 100% ALTS
        between    -> linear mix BTC<->ALTS

Writes: output/equity_curve_fg_dom.csv
"""

import os
import time
from datetime import datetime

import requests
import pandas as pd
import numpy as np

COINGECKO_BASE = "https://api.coingecko.com/api/v3"

# For free APIs, realistically last ~365 days
START_DATE = "2024-01-01"
END_DATE   = "2025-11-01"

OUT_CSV_EQUITY = "output/equity_curve_fg_dom.csv"
os.makedirs("output", exist_ok=True)

# Dominance thresholds
DOM_LOW   = 0.75
DOM_HIGH  = 0.81
DOM_MID_LOW  = 0.771
DOM_MID_HIGH = 0.789
GREED_STABLE_THRESHOLD = 77.0  # FG_lite threshold for stabling

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
    CoinGecko free plan: last 365d allowed.
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


def load_fg_lite(path="output/fg2_daily.csv"):
    df = pd.read_csv(path, parse_dates=["date"])
    df["date"] = df["date"].dt.date
    return df[["date", "FG_lite"]]


def allocation_from_dom_and_fg(btc_dom, fg_lite):
    """
    Returns target weights: dict(btc, alts, stables),
    BTC dominance in [0,1], FG_lite in [0,100].
    """
    # 1) Mid dominance -> 100% stables
    if DOM_MID_LOW <= btc_dom <= DOM_MID_HIGH:
        return {"btc": 0.0, "alts": 0.0, "stables": 1.0}

    # 2) Extreme greed -> 100% stables
    if fg_lite >= GREED_STABLE_THRESHOLD:
        return {"btc": 0.0, "alts": 0.0, "stables": 1.0}

    # 3) Otherwise: pure dominance rotation between BTC and ALTS
    if btc_dom <= DOM_LOW:
        btc_w, alt_w = 1.0, 0.0
    elif btc_dom >= DOM_HIGH:
        btc_w, alt_w = 0.0, 1.0
    else:
        t = (btc_dom - DOM_LOW) / (DOM_HIGH - DOM_LOW)
        btc_w = 1.0 - t
        alt_w = t

    return {"btc": btc_w, "alts": alt_w, "stables": 0.0}


# ---------- BUILD DATA ----------

def build_market_data():
    print("Fetching CoinGecko data for BTC, ETH, SOL, BNB…")
    frames = []
    for cid in RISK_IDS:
        df = fetch_cg_ohlc_and_mc(cid, START_DATE, END_DATE)
        frames.append(df)

    df = frames[0]
    for sub in frames[1:]:
        df = df.merge(sub, on="date", how="inner")

    df = df.sort_values("date").reset_index(drop=True)

    # BTC vs ALT bucket
    df["btc_mc"]  = df["bitcoin_mc"]
    df["alt_mc"]  = df["ethereum_mc"] + df["solana_mc"] + df["binancecoin_mc"]
    df["btc_dom"] = df["btc_mc"] / (df["btc_mc"] + df["alt_mc"])

    # Prices
    df["btc_price"] = df["bitcoin_price"]
    df["eth_price"] = df["ethereum_price"]
    df["sol_price"] = df["solana_price"]
    df["bnb_price"] = df["binancecoin_price"]

    return df


# ---------- BACKTEST ----------

def run_backtest():
    df_mkt = build_market_data()
    df_fg  = load_fg_lite()

    df = df_mkt.merge(df_fg, on="date", how="inner").sort_values("date").reset_index(drop=True)

    btc_units   = 0.0
    alt_units   = 0.0
    stable_usd  = 100.0  # start in stables
    equity_hist = []

    for i, row in df.iterrows():
        date      = row["date"]
        btc_price = row["btc_price"]
        alt_price = (row["ethereum_price"] + row["solana_price"] + row["binancecoin_price"]) / 3.0

        equity = (
            btc_units * btc_price +
            alt_units * alt_price +
            stable_usd
        )

        w = allocation_from_dom_and_fg(row["btc_dom"], row["FG_lite"])
        target_btc_usd   = equity * w["btc"]
        target_alt_usd   = equity * w["alts"]
        target_stable_usd = equity * w["stables"]

        btc_units   = target_btc_usd / btc_price if btc_price > 0 else 0.0
        alt_units   = target_alt_usd / alt_price if alt_price > 0 else 0.0
        stable_usd  = target_stable_usd

        if i == 0:
            btc_only_units = equity / btc_price
        btc_only_equity = btc_only_units * btc_price

        equity_hist.append({
            "date": date,
            "equity": equity,
            "btc_only": btc_only_equity,
            "btc_dom": row["btc_dom"],
            "FG_lite": row["FG_lite"],
            "w_btc": w["btc"],
            "w_alts": w["alts"],
            "w_stables": w["stables"],
        })

    res = pd.DataFrame(equity_hist)
    res.to_csv(OUT_CSV_EQUITY, index=False)

    print("\n=== SUMMARY ===")
    print("Start:", res["date"].iloc[0], "End:", res["date"].iloc[-1])
    print("Final equity (strategy):", res["equity"].iloc[-1])
    print("Final equity (BTC only):", res["btc_only"].iloc[-1])

    # Ensure date is a proper datetime index for resampling
    res_annual = res.copy()
    res_annual["date"] = pd.to_datetime(res_annual["date"])
    res_annual = res_annual.set_index("date")[["equity", "btc_only"]].resample("YE").last()

    print("\n=== ANNUAL ===")
    print(res_annual)


if __name__ == "__main__":
    run_backtest()
  
