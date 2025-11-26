#!/usr/bin/env python3
"""
dom_trade_plan.py

Reads dom_signals_hourly.json (from hourly_dom_algo.py) and produces
a high-level trade plan in dom_trade_plan.json.

This does NOT talk to Binance. It only describes:

- action: HOLD / FLATTEN_TO_STABLES / SWITCH
- target_type: STABLES / BTC / ALT
- target_token: NONE / BTC / SOL / etc.
- notes: human-readable summary

You will later plug this into your own execution script to place orders.
"""

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict

ROOT = Path(".")
DOCS = ROOT / "docs"
SIGNALS_FILE = ROOT / "dom_signals_hourly.json"
PLAN_FILE_ROOT = ROOT / "dom_trade_plan.json"
PLAN_FILE_DOCS = DOCS / "dom_trade_plan.json"


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def load_json(path: Path) -> Dict[str, Any]:
    return json.loads(path.read_text())


def main() -> None:
    if not SIGNALS_FILE.exists():
        print("[dom_trade_plan] dom_signals_hourly.json not found; aborting.")
        return

    sig = load_json(SIGNALS_FILE)
    action = sig.get("action", "HOLD")
    pos = sig.get("position", {}) or {}

    pos_type = (pos.get("type") or "").upper()
    pos_token = (pos.get("token") or "NONE").upper()
    hmi_override = bool(pos.get("hmi_override", False))

    target_type = pos_type
    target_token = pos_token

    # Interpret action + position into a simple plan
    if hmi_override:
        # HMI risk-off: always flatten to stables
        plan_action = "FLATTEN_TO_STABLES"
        target_type = "STABLES"
        target_token = "NONE"
        notes = "HMI override active; flatten to STABLES (USDC)."

    else:
        if action == "HOLD":
            plan_action = "HOLD"
            notes = f"Hold current position: {pos_type} {pos_token}."
        elif action == "FLATTEN_TO_STABLES":
            plan_action = "FLATTEN_TO_STABLES"
            target_type = "STABLES"
            target_token = "NONE"
            notes = "Flatten to STABLES (USDC). Sell all DOM tokens and BTC into USDC."
        elif action == "SWITCH":
            # Switch to whatever the new position is
            if pos_type == "STABLES":
                plan_action = "FLATTEN_TO_STABLES"
                target_type = "STABLES"
                target_token = "NONE"
                notes = "Switch result is STABLES; flatten all DOM tokens and BTC to USDC."
            else:
                plan_action = "SWITCH"
                target_type = pos_type
                target_token = pos_token
                notes = f"Rotate full equity into {pos_type} {pos_token} via USDC hub."
        else:
            plan_action = "HOLD"
            notes = f"Unknown action '{action}', defaulting to HOLD."

    plan = {
        "timestamp": now_iso(),
        "from_signals_timestamp": sig.get("timestamp"),
        "plan_action": plan_action,
        "target_type": target_type,
        "target_token": target_token,
        "notes": notes,
    }

    txt = json.dumps(plan, indent=2)
    PLAN_FILE_ROOT.write_text(txt)
    PLAN_FILE_DOCS.write_text(txt)

    print(f"[dom_trade_plan] Plan: action={plan_action}, target={target_type}/{target_token}")
    print(f"[dom_trade_plan] Notes: {notes}")


if __name__ == "__main__":
    main()
