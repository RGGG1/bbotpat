#!/usr/bin/env python3
"""
backfill_dom_mc_history_full.py

One-off (or occasional) backfill of BTC + alt market-cap history from CoinGecko.

- Fetches daily market caps for each token using:
    /coins/{id}/market_chart?vs_currency=usd&days=max

- Builds dom_mc_history.json in the same format as the existing file:

    {
      "tokens": ["BTC", "ETH", ...],
      "series": [
        { "date": "YYYY-MM-DD", "mc": {"BTC": ..., "ETH": ..., ...} },
        ...
      ]
    }

- Prints how many days of data we have per token.

Notes:
- CoinGecko public API may not go back a full 730 days for every coin,
  but `days=max` will give us the maximum available window.
"""

import json
import time
from datetime import datetime
from pathlib import Path

import requests

DOCS_ROOT = Path(".")
OUT_PATH = DOCS_ROOT / "dom_mc_history.json"

COINGECKO = "https://api.coingecko.com/api/v3"

# Mapping of our symbols -> CoinGecko IDs
IDS = {
    "BTC": "bitcoin",
    "ETH": "ethereum",
    "BNB": "binancecoin",
    "SOL": "solana",
    "DOGE": "dogecoin",
    "TON": "toncoin",
    "SUI": "sui",
    "UNI": "uniswap",
    # We can add stables later if we want, but the DOM logic only needs these 8.
}


def cg_get(path: str, params=None, timeout: int = 60):
    if params is None:
        params = {}
    url = COINGECKO + path
    r = requests.get(url, params=params, timeout=timeout)
    if r.status_code != 200:
        raise RuntimeError(f"CoinGecko error {r.status_code}: {r.text[:300]}")
    return r.json()


def fetch_mc_series(coin_id: str) -> dict:
    """
    Return { 'YYYY-MM-DD': market_cap_float, ... } for the given CoinGecko ID.
    Uses daily data via /market_chart?days=max.
    """
    js = cg_get(
        f"/coins/{coin_id}/market_chart",
        params={"vs_currency": "usd", "days": "max"},
    )

    data = js.get("market_caps", [])
    out = {}
    for ts_ms, mc in data:
        # CoinGecko returns many points per day; collapse to one per UTC date.
        dt = datetime.utcfromtimestamp(ts_ms / 1000.0).date().isoformat()
        try:
            mc_val = float(mc)
        except Exception:
            mc_val = 0.0
        # Keep the latest value for that date (or you could average)
        out[dt] = mc_val
    return out


def build_history():
    per_token = {}
    for sym, cid in IDS.items():
        print(f"[backfill] Fetching history for {sym} ({cid}) ...", flush=True)
        series = fetch_mc_series(cid)
        per_token[sym] = series
        print(f"[backfill]   {sym}: {len(series)} daily points", flush=True)
        time.sleep(1.2)  # be gentle with CG rate limits

    # Build the union of all dates where BTC has a value
    btc_dates = sorted(per_token["BTC"].keys())
    tokens = list(IDS.keys())

    series_out = []
    for date_str in btc_dates:
        mc_row = {}
        for sym in tokens:
            mc_row[sym] = float(per_token[sym].get(date_str, 0.0))
        series_out.append({"date": date_str, "mc": mc_row})

    payload = {
        "tokens": tokens,
        "series": series_out,
    }

    return payload, per_token


def main():
    payload, per_token = build_history()

    # Write JSON
    OUT_PATH.write_text(json.dumps(payload, indent=2))
    print(f"\n[backfill] Wrote {OUT_PATH} with {len(payload['series'])} days.")

    # Print per-token day counts (non-zero market caps)
    print("\n[backfill] Days with non-zero market cap per token:")
    for sym in sorted(IDS.keys()):
        series = per_token[sym]
        non_zero_days = sum(1 for v in series.values() if v > 0)
        print(f"  {sym}: {non_zero_days} days")

    print("\n[backfill] Done.")


if __name__ == "__main__":
    main()
