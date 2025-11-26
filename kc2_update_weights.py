#!/usr/bin/env python3
"""
kc2_update_weights.py

Bridge from DOM hourly model (Knifecatcher #2) to portfolio_weights.json.

- Reads:
      dom_signals_hourly.json
- Determines which asset KC2 wants to hold:
      STABLES (USDC), BTC, or one ALT.
- Writes:
      portfolio_weights.json

This file NEVER places trades.
It NEVER talks to Binance.
It ONLY updates weights.
"""

import json
from pathlib import Path
from typing import Any, Dict, Optional, List

ROOT = Path(".")
DOCS = ROOT / "docs"
PW_JSON_ROOT = ROOT / "portfolio_weights.json"

DOM_SIGNALS_CANDIDATES: List[Path] = [
    ROOT / "dom_signals_hourly.json",
    DOCS / "dom_signals_hourly.json",
]


def load_dom_signals() -> Optional[Dict[str, Any]]:
    for p in DOM_SIGNALS_CANDIDATES:
        if p.exists():
            try:
                return json.loads(p.read_text())
            except Exception:
                continue
    return None


def main() -> None:
    js = load_dom_signals()
    if not js:
        print("[kc2_update_weights] dom_signals_hourly.json not found or invalid; aborting.")
        return

    action = str(js.get("action", "HOLD")).upper()
    pos = js.get("position", {}) or {}
    pos_type = str(pos.get("type", "STABLES")).upper()
    pos_token = str(pos.get("token", "NONE")).upper()
    hmi_override = bool(pos.get("hmi_override", False))

    # Default: force STABLES if anything is weird
    target_asset = "STABLES"

    # HMI override forces USDC
    if hmi_override:
        target_asset = "STABLES"
    else:
        if pos_type == "STABLES":
            target_asset = "STABLES"
        elif pos_type == "BTC":
            target_asset = "BTC"
        elif pos_type == "ALT" and pos_token not in ("", "NONE"):
            target_asset = pos_token
        else:
            target_asset = "STABLES"

    weights_payload = {
        "timestamp": js.get("timestamp"),
        "hmi": js.get("hmi"),
        "hmi_band": js.get("hmi_band"),
        "weights": [
            {
                "asset": target_asset,
                "weight": 1.0,
            }
        ],
    }

    PW_JSON_ROOT.write_text(json.dumps(weights_payload, indent=2))
    print(
        f"[kc2_update_weights] Updated portfolio_weights.json -> "
        f"100% in {target_asset} (action={action}, pos={pos_type}/{pos_token}, hmi_override={hmi_override})"
    )


if __name__ == "__main__":
    main()
