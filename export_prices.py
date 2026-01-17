#!/usr/bin/env python3
"""
export_prices.py

Fetch live prices + market caps from CoinGecko and write:

    docs/prices_latest.json

rows:
  token, price, mc, change_24h, btc_dom, range
"""

import json
from datetime import datetime
from pathlib import Path

# Auto-managed universe (BTC + ALT_LIST from kc3_hmi_momentum_agent)
TOKENS = json.load(open("data/kc3_token_universe.json"))

import requests

DOCS = Path("docs")
DOCS.mkdir(exist_ok=True, parents=True)
OUT = DOCS / "prices_latest.json"

COINGECKO = "https://api.coingecko.com/api/v3"

TOKEN_IDS = {
    "BTC":  "bitcoin",
    "ETH":  "ethereum",
    "BNB":  "binancecoin",
    "SOL":  "solana",
    "DOGE": "dogecoin",
    "TON":  "the-open-network",
    "SUI":  "sui",
    "UNI":  "uniswap",
    "USDT": "tether",
    "USDC": "usd-coin",
}

DISPLAY_ORDER = ["BTC", "ETH", "BNB", "SOL", "DOGE", "TON", "USDTC", "SUI", "UNI"]


def cg_get(path, params=None, timeout=40):
    if params is None:
        params = {}
    url = COINGECKO + path
    r = requests.get(url, params=params, timeout=timeout)
    if r.status_code != 200:
        raise RuntimeError(f"CG error {r.status_code}: {r.text[:200]}")
    return r.json()


def main():
    ids = ",".join(TOKEN_IDS.values())
    health = {"coingecko_ok": True}

    try:
        js = cg_get(
            "/coins/markets",
            params={
                "vs_currency": "usd",
                "ids": ids,
                "order": "market_cap_desc",
                "per_page": 250,
                "page": 1,
                "sparkline": "false",
            },
        )
    except Exception as e:
        print("[prices] CoinGecko error:", e)
        health["coingecko_ok"] = False
        js = []

    by_id = {row["id"]: row for row in js}

    def safe_price(sym):
        row = by_id.get(TOKEN_IDS[sym])
        return float(row["current_price"]) if row else 0.0

    def safe_mc(sym):
        row = by_id.get(TOKEN_IDS[sym])
        return float(row["market_cap"]) if row else 0.0

    def safe_change(sym):
        row = by_id.get(TOKEN_IDS[sym])
        return float(row.get("price_change_percentage_24h") or 0.0) if row else 0.0

    btc_mc = safe_mc("BTC")
    rows = []

    # pre-compute stables combined
    usdt_mc = safe_mc("USDT")
    usdc_mc = safe_mc("USDC")
    usdctotal_mc = usdt_mc + usdc_mc

    for sym in DISPLAY_ORDER:
        if sym == "USDTC":
            price = 1.0
            mc = usdctotal_mc
            change = 0.0
            btc_dom = None
            rng = ""
        else:
            price = safe_price(sym)
            mc = safe_mc(sym)
            change = safe_change(sym)
            if sym == "BTC" or mc <= 0 or btc_mc <= 0:
                btc_dom = None
            else:
                btc_dom = round(100.0 * btc_mc / (btc_mc + mc), 1)
            rng = ""  # pairwise range per token – leave blank for now

        rows.append({
            "token": sym,
            "price": price,
            "mc": mc,
            "change_24h": change,
            "btc_dom": btc_dom,
            "range": rng,
        })

    out = {
        "timestamp": datetime.utcnow().isoformat() + "Z",
        "health": health,
        "rows": rows,
    }
    OUT.write_text(json.dumps(out, indent=2))
    print("[prices] wrote", OUT)


if __name__ == "__main__":
    main()
