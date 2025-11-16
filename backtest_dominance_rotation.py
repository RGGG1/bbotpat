#!/usr/bin/env python3
"""
'Hive' equity updater.

- BTC vs ALT bucket (ETH+SOL+BNB)
- Uses CoinGecko for daily prices/market caps (last ~365d)
- Uses HMI from output/fg2_daily.csv (stored as FG_lite)

Capital assumptions:

- Start with $100 only, entirely in stables.
- Start date: 2025-11-10 (first live Hive allocation day).
- No DCA, no further capital added.
- BTC-only benchmark also starts with $100 on the same date and just holds BTC.

We use the intersection of:
    * desired date range (2025-11-10 → today)
    * HMI (fg2) date range
If there is no overlap, we exit gracefully and leave the previous
equity_curve_fg_dom.csv untouched.
"""

import os
import time
import json
from datetime import datetime
from pathlib import Path

import requests
import pandas as pd
import numpy as np

COINGECKO_BASE = "https://api.coingecko.com/api/v3"

# Live start date for Hive:
DESIRED_START_DATE = "2025-11-10"
DESIRED_END_DATE   = datetime.utcnow().date().isoformat()

OUT_CSV_EQUITY = "output/equity_curve_fg_dom.csv"
DOM_BANDS_JSON = Path("dom_bands_latest.json")
os.makedirs("output", exist_ok=True)

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


def compute_dynamic_dom_bands(dom_series: pd.Series):
    """
    Compute dynamic dominance bands from a BTC dominance series in [0,1].

    Design (quantile-based, 35% / 30% / 35% mass split):
      - Bottom 35%  of dominance history  -> BTC side
      - Middle 30%  (35–65th percentile)  -> mid stables zone
      - Top 35%     of dominance history  -> ALT side

    We then linearly map BTC->ALTs across the side regions, with
    the mid-zone forced into 100% stables.

    Returns (DOM_MIN, DOM_35, DOM_65, DOM_MAX), which can be interpreted as:
      - dominance <= DOM_35 : BTC side
      - DOM_35–DOM_65       : mid-zone (stables)
      - dominance >= DOM_65 : ALTs side
    """
    s = dom_series.dropna()
    if len(s) < 50:
        raise RuntimeError("Not enough data to compute dynamic dominance bands (need >= 50 points).")

    # Robust min/max using near-extreme quantiles to avoid single-point outliers
    DOM_MIN = float(s.quantile(0.01))
    DOM_MAX = float(s.quantile(0.99))

    DOM_35 = float(s.quantile(0.35))
    DOM_65 = float(s.quantile(0.65))

    # Ensure ordering just in case of numerical ties
    DOM_MIN = min(DOM_MIN, DOM_35, DOM_65)
    DOM_MAX = max(DOM_MAX, DOM_35, DOM_65)

    return DOM_MIN, DOM_35, DOM_65, DOM_MAX


def allocation_from_dom_and_hmi(btc_dom, hmi, dom_bands):
    """
    Returns target weights: dict(btc, alts, stables),
    BTC dominance in [0,1], HMI in [0,100].

    dom_bands = (DOM_MIN, DOM_35, DOM_65, DOM_MAX)

    We interpret:
      - dominance <= DOM_35 : BTC side
      - DOM_35–DOM_65       : mid stables zone
      - dominance >= DOM_65 : ALTs side

    We *linearly* scale from 100% BTC at DOM_MIN
    to 100% ALTs at DOM_MAX, except that in the mid zone
    (DOM_35 < dom < DOM_65) we override to 100% stables.
    """
    DOM_MIN, DOM_35, DOM_65, DOM_MAX = dom_bands

    # 1) Greed override from HMI
    if hmi >= GREED_STABLE_THRESHOLD:
        return {"btc": 0.0, "alts": 0.0, "stables": 1.0}

    # 2) Mid stables zone
    if DOM_35 < btc_dom < DOM_65:
        return {"btc": 0.0, "alts": 0.0, "stables": 1.0}

    # Guard against degenerate cases
    if DOM_MAX <= DOM_MIN:
        # Fallback to equal weights in a degenerate scenario
        return {"btc": 0.5, "alts": 0.5, "stables": 0.0}

    # 3) Linear BTC->ALTs schedule ignoring mid-zone
    # Map dominance to [0,1] along the full dynamic range
    t_raw = (btc_dom - DOM_MIN) / (DOM_MAX - DOM_MIN)
    t_raw = max(0.0, min(1.0, t_raw))

    # Linear weights across the *entire* range:
    # t=0   -> 100% BTC, 0% ALTs
    # t=0.5 -> 50/50
    # t=1   -> 0% BTC, 100% ALTs
    btc_w = 1.0 - t_raw
    alt_w = t_raw

    # Outside the stables band we honour this linear schedule
    return {"btc": btc_w, "alts": alt_w, "stables": 0.0}


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

    # 3a) Dynamic dominance bands from full btc_dom history
    dom_bands = compute_dynamic_dom_bands(df["btc_dom"])
    DOM_MIN, DOM_35, DOM_65, DOM_MAX = dom_bands
    print("Dynamic BTC dominance bands (BTC side / stables / ALTs side):")
    print(f"  DOM_MIN={DOM_MIN:.4f}, DOM_35={DOM_35:.4f}, "
          f"DOM_65={DOM_65:.4f}, DOM_MAX={DOM_MAX:.4f}")

    # Persist bands for the website / monitoring
    bands_payload = {
        "as_of": eff_end_str,
        "pair": "btc_vs_ethbnbsol",
        "dom_min": DOM_MIN,
        "dom_35": DOM_35,
        "dom_65": DOM_65,
        "dom_max": DOM_MAX,
    }
    DOM_BANDS_JSON.write_text(json.dumps(bands_payload, indent=2))
    print(f"Wrote {DOM_BANDS_JSON} with dynamic dominance bands.")

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

        # Decide target allocation from dominance + HMI using dynamic bands
        w = allocation_from_dom_and_hmi(row["btc_dom"], row["HMI"], dom_bands)
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
