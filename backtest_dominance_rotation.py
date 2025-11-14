#!/usr/bin/env python3
"""
'Hive' equity updater.

- BTC vs ALT bucket (ETH+SOL+BNB)
- Uses CoinGecko for daily prices/market caps (last ~365d)
- Uses HMI from output/fg2_daily.csv (stored as FG_lite)

Capital assumptions:

- Start with $100 only, entirely in stables.
- No DCA, no further capital added.
- First allocation happens on the first backtest date.
- BTC-only benchmark also starts with $100 on the same date and just holds BTC.

Allocation logic (must match live Telegram logic):

1) Greed override:
   If HMI >= GREED_STABLE_THRESHOLD (77) -> 100% STABLES

2) Stable mid-zone (dominance pivot):
   If DOM_MID_LOW < dom < DOM_MID_HIGH (0.771–0.789) -> 100% STABLES

3) BTC side:
   - If dom <= DOM_LOW (0.75) -> 100% BTC
   - If DOM_LOW < dom <= DOM_MID_LOW (0.75–0.771):
       BTC/ALTs change linearly:
         dom = 0.75   -> 100% BTC / 0% ALTs
         dom = 0.771  -> 0% BTC / 100% ALTs

4) ALT side:
   - If dom >= DOM_HIGH (0.81) -> 100% ALTs
   - If DOM_MID_HIGH <= dom < DOM_HIGH (0.789–0.81):
       BTC/ALTs change linearly:
         dom = 0.789 -> 100% BTC / 0% ALTs
         dom = 0.81  -> 0% BTC / 100% ALTs
"""

import os
import time
from datetime import datetime, date

import requests
import pandas as pd
import numpy as np

COINGECKO_BASE = "https://api.coingecko.com/api/v3"

# Start backtest: you said we start with $100 on 14/11/25 and first trade at 00:02 on 15th.
START_DATE = "2025-11-15"

# END_DATE = today (UTC)
END_DATE = datetime.utcnow().date().isoformat()

OUT_CSV_EQUITY = "output/equity_curve_fg_dom.csv"
os.makedirs("output", exist_ok=True)

# Dominance thresholds (match live script)
DOM_LOW       = 0.75
DOM_HIGH      = 0.81
DOM_MID_LOW   = 0.771
DOM_MID_HIGH  = 0.789
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


def load_hmi(path="output/fg2_daily.csv"):
    df = pd.read_csv(path, parse_dates=["date"])
    df["date"] = df["date"].dt.date
    # FG_lite column is our HMI
    return df[["date", "FG_lite"]].rename(columns={"FG_lite": "HMI"})


def allocation_from_dom_and_hmi(btc_dom, hmi):
    """
    Returns target weights: dict(btc, alts, stables),
    BTC dominance in [0,1], HMI in [0,100].
    """

    # 1) Greed override
    if hmi >= GREED_STABLE_THRESHOLD:
        return {"btc": 0.0, "alts": 0.0, "stables": 1.0}

    # 2) Stable mid-zone (open interval)
    if DOM_MID_LOW < btc_dom < DOM_MID_HIGH:
        return {"btc": 0.0, "alts": 0.0, "stables": 1.0}

    # 3) Extremes: pure BTC or pure ALTs
    if btc_dom <= DOM_LOW:
        return {"btc": 1.0, "alts": 0.0, "stables": 0.0}
    if btc_dom >= DOM_HIGH:
        return {"btc": 0.0, "alts": 1.0, "stables": 0.0}

    # 4) BTC side linear: DOM_LOW < dom <= DOM_MID_LOW
    if btc_dom <= DOM_MID_LOW:
        t = (btc_dom - DOM_LOW) / (DOM_MID_LOW - DOM_LOW)
        btc_w = 1.0 - t
        alt_w = t
        return {"btc": btc_w, "alts": alt_w, "stables": 0.0}

    # 5) ALT side linear: DOM_MID_HIGH <= dom < DOM_HIGH
    if btc_dom >= DOM_MID_HIGH:
        t = (btc_dom - DOM_MID_HIGH) / (DOM_HIGH - DOM_MID_HIGH)
        btc_w = 1.0 - t
        alt_w = t
        return {"btc": btc_w, "alts": alt_w, "stables": 0.0}

    # Fallback
    return {"btc": 0.0, "alts": 0.0, "stables": 1.0}


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


# ---------- BACKTEST / EQUITY UPDATE ----------

def run_backtest():
    df_mkt = build_market_data()
    df_hmi = load_hmi()

    df = df_mkt.merge(df_hmi, on="date", how="inner").sort_values("date").reset_index(drop=True)
    if df.empty:
        raise RuntimeError("No overlapping dates between market data and HMI; check START_DATE and HMI file.")

    # Portfolio starts all in stables
    btc_units   = 0.0
    alt_units   = 0.0
    stable_usd  = INITIAL_CAPITAL

    equity_hist = []

    # BTC-only: invest 100 in BTC on the first day, then hold
    btc_only_units = None

    for i, row in df.iterrows():
        date_row = row["date"]
        btc_price = row["btc_price"]
        alt_price = (row["ethereum_price"] + row["solana_price"] + row["binancecoin_price"]) / 3.0

        # Current equity
        equity = btc_units * btc_price + alt_units * alt_price + stable_usd

        # BTC-only benchmark: buy once on the first day
        if btc_only_units is None:
            btc_only_units = INITIAL_CAPITAL / btc_price
        btc_only_equity = btc_only_units * btc_price

        # Decide target allocation from dominance + HMI
        w = allocation_from_dom_and_hmi(row["btc_dom"], row["HMI"])
        target_btc_usd    = equity * w["btc"]
        target_alt_usd    = equity * w["alts"]
        target_stable_usd = equity * w["stables"]

        # Rebalance
        btc_units   = target_btc_usd / btc_price if btc_price > 0 else 0.0
        alt_units   = target_alt_usd / alt_price if alt_price > 0 else 0.0
        stable_usd  = target_stable_usd

        equity_hist.append({
            "date": date_row,
            "equity": equity,
            "btc_only": btc_only_equity,
            "btc_dom": row["btc_dom"],
            "HMI": row["HMI"],
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


if __name__ == "__main__":
    run_backtest()
   
