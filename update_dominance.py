#!/usr/bin/env python3
"""
update_dominance.py

Uses:
- docs/dom_bands_latest.json   (for min_pct, max_pct – BTC vs EthBnbSol)
- docs/hmi_latest.json         (for latest HMI)
- CoinGecko                    (for live BTC / EthBnbSol market caps)
- dom_mc_history.json          (for per-token market-cap history)
- docs/prices_latest.json      (for token rows)

Writes:
- docs/dom_bands_latest.json:
    - min_pct, max_pct
    - btc_pct, alt_pct
    - btc_mc_fmt, alt_mc_fmt
    - action  (Buy BTC / Buy ALTs / Stable up)

- docs/prices_latest.json:
    - per-token:
        - btc_dom     (current BTC dominance vs that token)
        - range       ("low–high%" dominance band from ~730-day history)
        - dom_action  ("ALT/BTC split", e.g. "38/62")
        - dom_bias    ("ALT favoured" / "BTC favoured" / "Neutral")
"""

from datetime import datetime
from pathlib import Path
import json
import requests

DOCS = Path("docs")
DOCS.mkdir(exist_ok=True, parents=True)
BANDS = DOCS / "dom_bands_latest.json"
HMI = DOCS / "hmi_latest.json"
PRICES = DOCS / "prices_latest.json"

# History of per-token market caps
DOM_MC_HISTORY_ROOT = Path("dom_mc_history.json")
DOM_MC_HISTORY_DOCS = DOCS / "dom_mc_history.json"

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


def load_dom_mc_history():
    """
    Load dom_mc_history.json from root or docs/.
    Expected structure:

    {
      "series": [
        {
          "date": "YYYY-MM-DD",
          "mc": {
            "BTC": ...,
            "ETH": ...,
            ...
          }
        },
        ...
      ]
    }
    """
    for path in (DOM_MC_HISTORY_DOCS, DOM_MC_HISTORY_ROOT):
        if path.exists():
            try:
                return json.loads(path.read_text())
            except Exception:
                continue
    return None


def enrich_prices_with_dom_ranges():
    """
    Use dom_mc_history.json to compute per-token BTC dominance history and
    inject per-token dominance bands into docs/prices_latest.json:

        - btc_dom: current dominance (today)
        - range: "low–high%" over history (rolling window)
        - dom_action: "ALT/BTC" split, e.g. "38/62"
        - dom_bias: "ALT favoured" / "BTC favoured" / "Neutral"
    """
    if not PRICES.exists():
        return

    try:
        prices_js = json.loads(PRICES.read_text())
    except Exception:
        return

    rows = prices_js.get("rows", [])
    if not rows:
        return

    hist = load_dom_mc_history()
    if not hist or "series" not in hist:
        return

    series = hist["series"]
    if not series:
        return

    # Build per-token dominance series: BTC / (BTC + token)
    token_dom_history = {}  # token -> [dom values over time]
    for entry in series:
        mc = entry.get("mc") or {}
        btc = mc.get("BTC")
        if not btc or btc <= 0:
            continue
        for t, v in mc.items():
            if t == "BTC":
                continue
            if v and v > 0:
                dom_val = 100.0 * btc / (btc + v)
                token_dom_history.setdefault(t.upper(), []).append(dom_val)

    latest_mc = series[-1].get("mc") or {}
    btc_latest = latest_mc.get("BTC", 0.0)
    if btc_latest <= 0:
        return

    # Enrich each price row with dominance info where available
    for row in rows:
        token = str(row.get("token", "")).upper()
        if token not in token_dom_history:
            continue

        hist_vals = token_dom_history[token]
        if not hist_vals:
            continue

        dom_low = min(hist_vals)
        dom_high = max(hist_vals)
        if dom_high <= dom_low:
            continue

        token_mc_latest = latest_mc.get(token, 0.0)
        if token_mc_latest <= 0:
            continue

        dom_current = 100.0 * btc_latest / (btc_latest + token_mc_latest)

        # Decide decimals based on band width
        width = dom_high - dom_low
        if width < 0.02:
            dec = 4
        elif width < 0.1:
            dec = 3
        elif width < 1.0:
            dec = 2
        else:
            dec = 1

        range_str = f"{dom_low:.{dec}f}–{dom_high:.{dec}f}%"

        # Normalised position in band
        z = (dom_current - dom_low) / (dom_high - dom_low)
        if z < 0.0:
            z = 0.0
        elif z > 1.0:
            z = 1.0

        # ALT/BTC split – ALT first, BTC second
        alt_pct = round((1.0 - z) * 100.0)
        if alt_pct < 0:
            alt_pct = 0
        if alt_pct > 100:
            alt_pct = 100
        btc_pct = 100 - alt_pct
        split_str = f"{alt_pct}/{btc_pct}"

        # Bias label using 40/20/40 bands
        span = dom_high - dom_low
        if span > 0:
            neutral_low = dom_low + 0.40 * span
            neutral_high = dom_low + 0.60 * span
            if dom_current < neutral_low:
                bias = "ALT favoured"
            elif dom_current > neutral_high:
                bias = "BTC favoured"
            else:
                bias = "Neutral"
        else:
            bias = "Neutral"

        row["btc_dom"] = round(dom_current, 1)
        row["range"] = range_str
        # Action column should show the split:
        row["dom_action"] = split_str
        # Keep a label in case we want it later:
        row["dom_bias"] = bias

    prices_js["rows"] = rows
    PRICES.write_text(json.dumps(prices_js, indent=2))
    print("[dom] Updated prices_latest.json with per-token dominance bands + ALT/BTC splits.")


def main():
    # ------------------------------------------------------------------
    # 1) Global BTC vs EthBnbSol dominance band (existing logic)
    # ------------------------------------------------------------------
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

    # Live market caps (BTC vs ETH+BNB+SOL)
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
    print(
        f"Wrote {BANDS} with dom={payload['btc_pct']}%, "
        f"range={payload['min_pct']}–{payload['max_pct']}%, action={action}"
    )

    # ------------------------------------------------------------------
    # 2) Per-token bands for the token table & KC2 (dynamic, from history)
    # ------------------------------------------------------------------
    enrich_prices_with_dom_ranges()


if __name__ == "__main__":
    main()
