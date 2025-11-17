#!/usr/bin/env python3
"""
update_dominance.py

Uses:
- docs/dom_bands_latest.json   (for min_pct, max_pct)
- docs/hmi_latest.json         (for latest HMI)
- CoinGecko                    (for live market caps)

Writes back to docs/dom_bands_latest.json:
- min_pct, max_pct
- btc_pct, alt_pct
- btc_mc_fmt, alt_mc_fmt
- action
"""

from datetime import datetime
from pathlib import Path
import json
import requests

DOCS = Path("docs")
DOCS.mkdir(exist_ok=True, parents=True)
BANDS = DOCS / "dom_bands_latest.json"
HMI = DOCS / "hmi_latest.json"

COINGECKO = "https://api.coingecko.com/api/v3"

IDS = {
    "BTC": "bitcoin",
    "ETH": "ethereum",
    "BNB": "binancecoin",
    "SOL": "solana",
}

GREED_HMI_THRESHOLD = 77.0


def cg_get(path, params=None, timeout=40):
    if params is None:
        params = {}
    url = COINGECKO + path
    r = requests.get(url, params=params, timeout=timeout)
    if r.status_code != 200:
        raise RuntimeError(f"CG error {r.status_code}: {r.text[:200]}")
    return r.json()


def fmt_mc(v):
    if v <= 0:
        return "$0"
    if v >= 1e12:
        return f"${v/1e12:.1f}T"
    if v >= 1e9:
        return f"${v/1e9:.1f}B"
    if v >= 1e6:
        return f"${v/1e6:.1f}M"
    return f"${int(v):,}"


def compute_action(dom_pct, min_pct, max_pct, hmi):
    # HMI override
    if hmi is not None and hmi >= GREED_HMI_THRESHOLD:
        return "Stable up"

    span = max_pct - min_pct
    if span <= 0:
        return "Stable up"

    low35 = min_pct + 0.35 * span
    high65 = min_pct + 0.65 * span

    if low35 <= dom_pct <= high65:
        return "Stable up"
    if dom_pct < low35:
        return "Buy BTC"
    return "Buy ALTs"


def main():
    # Load range
    min_pct = 70.0
    max_pct = 85.0
    if BANDS.exists():
        try:
            js = json.loads(BANDS.read_text())
            if isinstance(js, dict):
                if js.get("min_pct") is not None:
                    min_pct = float(js["min_pct"])
                if js.get("max_pct") is not None:
                    max_pct = float(js["max_pct"])
        except Exception:
            pass

    # Load HMI
    hmi = None
    if HMI.exists():
        try:
            hj = json.loads(HMI.read_text())
            hmi = float(hj.get("hmi"))
        except Exception:
            hmi = None

    # Live market caps
    ids = ",".join(IDS.values())
    js = cg_get(
        "/coins/markets",
        params={
            "vs_currency": "usd",
            "ids": ids,
            "order": "market_cap_desc",
            "per_page": 10,
            "page": 1,
            "sparkline": "false",
        },
    )
    by_id = {row["id"]: row for row in js}

    def mc(sym):
        row = by_id.get(IDS[sym])
        return float(row["market_cap"]) if row and row.get("market_cap") else 0.0

    btc_mc = mc("BTC")
    alt_mc = mc("ETH") + mc("BNB") + mc("SOL")

    if btc_mc + alt_mc <= 0:
        raise SystemExit("No market cap data for BTC or EthBnbSol")

    btc_pct = 100.0 * btc_mc / (btc_mc + alt_mc)
    alt_pct = 100.0 - btc_pct

    action = compute_action(btc_pct, min_pct, max_pct, hmi)

    payload = {
        "timestamp": datetime.utcnow().isoformat() + "Z",
        "btc_pct": round(btc_pct, 1),
        "alt_pct": round(alt_pct, 1),
        "min_pct": round(min_pct, 1),
        "max_pct": round(max_pct, 1),
        "btc_mc_fmt": fmt_mc(btc_mc),
        "alt_mc_fmt": fmt_mc(alt_mc),
        "action": action,
    }

    BANDS.write_text(json.dumps(payload, indent=2))
    print(f"Wrote {BANDS} with dom={payload['btc_pct']}%, range={min_pct}–{max_pct}%, action={action}")


if __name__ == "__main__":
    main()
