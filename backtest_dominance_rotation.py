#!/usr/bin/env python3
"""
'Hive' equity updater.

- BTC vs ALT bucket (ETH+SOL+BNB)
- Uses CoinGecko for daily prices/market caps (last ~365d)
- Uses HMI from output/fg2_daily.csv (stored as FG_lite)

Capital assumptions:

- Start with $100 only, entirely in stables.
- No DCA, no further capital added.
- BTC-only benchmark also starts with $100 on the same date and just holds BTC.

We use the intersection of:
    * desired date range
    * HMI (fg2) date range
If there is no overlap, we exit gracefully and leave the previous
equity_curve_fg_dom.csv untouched.
"""

import os
import time
from datetime import datetime

import requests
import pandas as pd
import numpy as np

COINGECKO_BASE = "https://api.coingecko.com/api/v3"

# We want to start as far back as reasonably possible.
DESIRED_START_DATE = "2023-01-01"
DESIRED_END_DATE   = datetime.utcnow().date().isoformat()

OUT_CSV_EQUITY = "output/equity_curve_fg_dom.csv"
os.makedirs("output", exist_ok=True)

# Dominance thresholds (must match HMI / TG script)
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

    We pass `days=365` (CoinGecko free limit), then filter records
    to [start_date, end_date].
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
    """
    Load HMI (FG_lite) and return df, min_date, max_date.
    """
    if not os.path.exists(path):
        raise RuntimeError("HMI file not found; run compute_fg2_index.py first.")

    df = pd.read_csv(path, parse_dates=["date"])
    if df.empty:
        raise RuntimeError("HMI file is empty; run compute_fg2_index.py first.")

    df["date"] = df["date"].dt.date
    df = df.sort_values("date")

    dmin = df["date"].min()
    dmax = df["date"].max()

    # FG_lite column is our HMI
    df = df[["date", "FG_lite"]].rename(columns={"FG_lite": "HMI"})
    return df, dmin, dmax


def allocation_from_dom_and_hmi(btc_dom, hmi):
    """
    Returns target weights: dict(btc, alts, stables),
    BTC dominance in [0,1], HMI in [0,100].

    1) HMI >= 77 -> 100% stables
    2) 0.771 < dom < 0.789 -> 100% stables
    3) dom <= 0.75 -> 100% BTC
    4) dom >= 0.81 -> 100% ALTs
    5) BTC side linear between 0.75–0.771
    6) ALT side linear between 0.789–0.81
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


# ---------- BUILD MARKET DATA ----------

def build_market_data(eff_start_str, eff_end_str):
    print(f"Fetching CoinGecko data for BTC, ETH, SOL, BNB… "
          f"({eff_start_str} → {eff_end_str})")

    frames = []
    for cid in RISK_IDS:
        df = fetch_cg_ohlc_and_mc(cid, eff_start_str, eff_end_str)
        frames.append(df)

    if not frames:
        raise RuntimeError("No market data frames returned from CoinGecko.")

    df = frames[0]
    for sub in frames[1:]:
        df = df.merge(sub, on="date", how="inner")

    if df.empty:
        raise RuntimeError("No overlapping market data between assets.")

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
    # 1) Load HMI and its range
    df_hmi, hmi_min, hmi_max = load_hmi()

    desired_start = datetime.strptime(DESIRED_START_DATE, "%Y-%m-%d").date()
    desired_end   = datetime.strptime(DESIRED_END_DATE, "%Y-%m-%d").date()

    # Effective intersection
    eff_start = max(desired_start, hmi_min)
    eff_end   = min(desired_end, hmi_max)

    if eff_start > eff_end:
        print(f"[Hive equity updater] No overlapping dates between desired "
              f"range ({desired_start}→{desired_end}) and HMI "
              f"range ({hmi_min}→{hmi_max}).")
        print("Leaving existing equity_curve_fg_dom.csv untouched.")
        return

    eff_start_str = eff_start.isoformat()
    eff_end_str   = eff_end.isoformat()

    # 2) Market data for effective range
    df_mkt = build_market_data(eff_start_str, eff_end_str)

    # 3) Merge market + HMI
    df = df_mkt.merge(df_hmi, on="date", how="inner").sort_values("date").reset_index(drop=True)
    if df.empty:
        print("[Hive equity updater] After merging market data and HMI, no rows remain.")
        print("Leaving existing equity_curve_fg_dom.csv untouched.")
        return

    print(f"[Hive equity updater] Effective backtest range: "
          f"{df['date'].iloc[0]} → {df['date'].iloc[-1]}")

    # Portfolio starts all in stables
    btc_units   = 0.0
    alt_units   = 0.0
    stable_usd  = INITIAL_CAPITAL

    equity_hist = []

    # BTC-only: invest 100 in BTC on the first day, then hold
    btc_only_units = None

    for _, row in df.iterrows():
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
       
