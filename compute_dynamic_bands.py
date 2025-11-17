#!/usr/bin/env python3
"""
compute_dynamic_bands.py

Compute 2-year BTC dominance min/max for:
    BTC vs EthBnbSol (ETH + BNB + SOL)

Writes:
    docs/dom_bands_latest.json   with keys: min_pct, max_pct
"""

from datetime import datetime
from pathlib import Path
import requests
import pandas as pd

DOCS = Path("docs")
DOCS.mkdir(exist_ok=True, parents=True)
OUT = DOCS / "dom_bands_latest.json"

COINGECKO = "https://api.coingecko.com/api/v3"
DAYS = 730

IDS = {
    "BTC": "bitcoin",
    "ETH": "ethereum",
    "BNB": "binancecoin",
    "SOL": "solana",
}


def cg_get(path, params=None, timeout=60):
    if params is None:
        params = {}
    url = COINGECKO + path
    r = requests.get(url, params=params, timeout=timeout)
    if r.status_code != 200:
        raise RuntimeError(f"CoinGecko error {r.status_code}: {r.text[:200]}")
    return r.json()


def fetch_mc(coin_id: str) -> pd.DataFrame:
    js = cg_get(
        f"/coins/{coin_id}/market_chart",
        params={"vs_currency": "usd", "days": str(DAYS)},
    )
    data = js.get("market_caps", [])
    if not data:
        return pd.DataFrame(columns=["date", "mc"])
    df = pd.DataFrame(data, columns=["ts", "mc"])
    df["date"] = df["ts"].apply(lambda t: datetime.utcfromtimestamp(t/1000.0).date())
    return df[["date", "mc"]].drop_duplicates("date").sort_values("date")


def main():
    frames = {}
    for sym, cid in IDS.items():
        frames[sym] = fetch_mc(cid)

    # Merge on date
    df = frames["BTC"].rename(columns={"mc": "btc_mc"})
    for sym in ["ETH", "BNB", "SOL"]:
        df = df.merge(
            frames[sym].rename(columns={"mc": f"{sym.lower()}_mc"}),
            on="date",
            how="inner"
        )

    if df.empty:
        raise SystemExit("No overlapping MC data for BTC/ETH/BNB/SOL")

    df["alt_mc"] = df["eth_mc"] + df["bnb_mc"] + df["sol_mc"]
    df = df[df["btc_mc"] + df["alt_mc"] > 0]

    df["dom"] = df["btc_mc"] / (df["btc_mc"] + df["alt_mc"])
    min_pct = round(df["dom"].min() * 100, 1)
    max_pct = round(df["dom"].max() * 100, 1)

    payload = {
        "min_pct": min_pct,
        "max_pct": max_pct
    }
    OUT.write_text(pd.io.json.dumps(payload, indent=2))
    print(f"Wrote {OUT} with 2-yr range {min_pct}%–{max_pct}%")


if __name__ == "__main__":
    main()
