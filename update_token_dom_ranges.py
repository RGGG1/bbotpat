#!/usr/bin/env python3
"""
update_token_dom_ranges.py

Use dom_mc_history.json (730-day rolling mc for BTC & alts from
backfill_dom_mc_history_from_csv.py) to compute per-token BTC dominance
bands and write them into docs/prices_latest.json.

For each ALT in UNIVERSE we:

- Build a series of   dom = 100 * BTC_MC / (BTC_MC + ALT_MC)
  using only days where BOTH BTC_MC > 0 and ALT_MC > 0.
- Compute min/max of that series (rounded to 1 decimal) -> "range".
- Compute a simple textual "dom_action":
      * "BTC favoured"   when BTC dominance is low in the band
      * "Stables"        around the middle of the band
      * "ALT favoured"   when BTC dominance is high in the band

We DO NOT touch the live btc_dom value, which comes from export_prices.py.
"""

from pathlib import Path
import json
from typing import Dict, List, Tuple, Optional

ROOT = Path(".")
DOCS = ROOT / "docs"

HISTORY = ROOT / "dom_mc_history.json"
PRICES = DOCS / "prices_latest.json"

# Tokens we want per-pair BTC vs ALT ranges for
UNIVERSE = ["ETH", "BNB", "SOL", "DOGE", "SUI", "UNI", "TON"]


def load_history() -> List[Dict]:
    if not HISTORY.exists():
        raise SystemExit(f"{HISTORY} not found; run backfill_dom_mc_history_from_csv.py first.")
    js = json.loads(HISTORY.read_text())
    series = js.get("series", [])
    if not isinstance(series, list) or not series:
        raise SystemExit(f"{HISTORY} has no 'series' data.")
    return series


def build_dom_ranges(series: List[Dict]) -> Dict[str, Tuple[float, float]]:
    """
    For each token in UNIVERSE, compute BTC dominance over the 730-day window:

        dom = 100 * BTC_MC / (BTC_MC + ALT_MC)

    using ONLY days where BTC_MC > 0 and ALT_MC > 0.
    """
    dom_series: Dict[str, List[float]] = {sym: [] for sym in UNIVERSE}

    for row in series:
        mc = row.get("mc", {}) or {}
        try:
            btc_mc = float(mc.get("BTC", 0.0) or 0.0)
        except Exception:
            btc_mc = 0.0
        if btc_mc <= 0:
            # skip days where BTC has no valid MC
            continue

        for sym in UNIVERSE:
            try:
                alt_mc = float(mc.get(sym, 0.0) or 0.0)
            except Exception:
                alt_mc = 0.0
            if alt_mc <= 0:
                # skip days where token has no valid MC
                continue

            dom = 100.0 * btc_mc / (btc_mc + alt_mc)
            dom_series[sym].append(dom)

    ranges: Dict[str, Tuple[float, float]] = {}
    for sym, vals in dom_series.items():
        if not vals:
            continue
        lo = min(vals)
        hi = max(vals)
        ranges[sym] = (round(lo, 1), round(hi, 1))

    return ranges


def parse_float(x) -> Optional[float]:
    if x is None:
        return None
    if isinstance(x, (int, float)):
        return float(x)
    try:
        f = float(str(x))
    except Exception:
        return None
    return f


def compute_dom_action(dom_now: Optional[float],
                       band: Optional[Tuple[float, float]]) -> str:
    """
    Simple 40 / 20 / 40 style mapping for textual action only.

    dom_now is BTC dominance (%), band is (min, max) of BTC dominance.

    We interpret:

        - low end of band  -> BTC relatively expensive vs ALT (BTC-favoured)
        - high end of band -> BTC relatively cheap vs ALT (ALT-favoured)

    Mapping:
        pos < 0.4     -> "BTC favoured"
        0.4–0.6       -> "Stables"
        pos > 0.6     -> "ALT favoured"
    """
    if dom_now is None or band is None:
        return ""

    lo, hi = band
    if hi <= lo:
        return ""

    pos = (dom_now - lo) / (hi - lo)
    if not (pos == pos):  # NaN check
        return ""

    # clamp to [0, 1]
    if pos < 0.0:
        pos = 0.0
    if pos > 1.0:
        pos = 1.0

    if 0.4 <= pos <= 0.6:
        return "Stables"
    if pos < 0.4:
        return "BTC favoured"
    return "ALT favoured"


def main() -> None:
    series = load_history()
    ranges = build_dom_ranges(series)

    if not PRICES.exists():
        raise SystemExit(f"{PRICES} not found; run export_prices.py first.")
    prices_js = json.loads(PRICES.read_text())
    rows = prices_js.get("rows", [])
    if not isinstance(rows, list):
        raise SystemExit("prices_latest.json has no 'rows' array.")

    updated = []

    for row in rows:
        token = str(row.get("token", "")).upper()
        if not token or token not in ranges:
            # leave BTC / stables / other tokens alone
            continue

        band = ranges.get(token)
        if band:
            lo, hi = band
            row["range"] = f"{lo:.1f}–{hi:.1f}%"
        else:
            row["range"] = ""

        dom_now = parse_float(row.get("btc_dom"))
        action = compute_dom_action(dom_now, band)
        if action:
            row["dom_action"] = action
        else:
            row["dom_action"] = ""

        updated.append((token, band, dom_now, action))

    prices_js["rows"] = rows
    PRICES.write_text(json.dumps(prices_js, indent=2))

    print("Updated docs/prices_latest.json with BTC-vs-ALT dominance ranges:")
    for token, band, dom_now, action in updated:
        if not band:
            print(f"  {token}: (no valid MC data)")
            continue
        lo, hi = band
        print(f"  {token}: {lo:.1f}–{hi:.1f}% (current {dom_now if dom_now is not None else 'n/a'}%, action='{action}')")


if __name__ == "__main__":
    main()
