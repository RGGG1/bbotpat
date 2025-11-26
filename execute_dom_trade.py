#!/usr/bin/env python3
"""
execute_dom_trade.py  (EXECUTION STUB + LIVE PLUG-IN HOOK)

Reads dom_trade_plan.json and determines what trades *should* be executed
for the DOM strategy.

IMPORTANT:
- This script does NOT talk to Binance by itself.
- It first tries to import live functions from dom_live_execution.py:
    sell_all_to_usdc(token)
    buy_with_all_usdc(target_token)
- If that import fails, it falls back to STUB implementations that only
  print what they would do.

Logic:
- If plan_action == HOLD:
    -> do nothing
- If plan_action == FLATTEN_TO_STABLES:
    -> sell all DOM tokens + BTC into USDC
- If plan_action == SWITCH to target ALT/BTC:
    -> sell all DOM tokens + BTC into USDC
    -> buy target token with 100% of USDC balance
"""

import json
from pathlib import Path
from typing import Any, Dict, List

ROOT = Path(".")
PLAN_FILE = ROOT / "dom_trade_plan.json"

# Universe of tokens the DOM strategy is allowed to touch
DOM_TOKENS = ["BTC", "ETH", "BNB", "SOL", "DOGE", "TON", "SUI", "UNI"]
STABLE_SYMBOL = "USDC"

# Try to import live execution functions, fall back to safe stubs if not available
LIVE_EXECUTION_AVAILABLE = False
try:
    # You will implement these in dom_live_execution.py when ready to go live
    from dom_live_execution import sell_all_to_usdc, buy_with_all_usdc  # type: ignore
    LIVE_EXECUTION_AVAILABLE = True
except Exception:
    # ===== STUB FUNCTIONS =====
    # Replace the bodies of these with your REAL Binance calls in dom_live_execution.py later.
    def sell_all_to_usdc(token: str) -> None:
        """
        Stub: Replace with real market sell implementation in dom_live_execution.py
        """
        print(f"[EXEC_STUB] Would SELL ALL {token} -> {STABLE_SYMBOL} via MARKET order.")

    def buy_with_all_usdc(target_token: str) -> None:
        """
        Stub: Replace with real market buy implementation in dom_live_execution.py
        """
        print(f"[EXEC_STUB] Would BUY {target_token} with ALL {STABLE_SYMBOL} balance via MARKET order.")
    # ==========================


def load_json(path: Path) -> Dict[str, Any]:
    return json.loads(path.read_text())


def main() -> None:
    if not PLAN_FILE.exists():
        print("[execute_dom_trade] dom_trade_plan.json not found; aborting.")
        return

    plan = load_json(PLAN_FILE)
    plan_action = plan.get("plan_action", "HOLD")
    target_type = (plan.get("target_type") or "").upper()
    target_token = (plan.get("target_token") or "NONE").upper()
    notes = plan.get("notes", "")

    print(f"[execute_dom_trade] Plan action={plan_action}, target={target_type}/{target_token}")
    if notes:
        print(f"[execute_dom_trade] Notes: {notes}")

    if not LIVE_EXECUTION_AVAILABLE:
        print("[execute_dom_trade] WARNING: Live execution plug-in not available.")
        print("[execute_dom_trade] Using STUB mode (no real trades will be placed).")

    # HOLD -> nothing to do
    if plan_action == "HOLD":
        print("[execute_dom_trade] HOLD: No trades to execute.")
        return

    # Build list of tokens we consider part of the DOM universe to be flattened
    tokens_to_sell: List[str] = []
    for t in DOM_TOKENS:
        if t != STABLE_SYMBOL:
            tokens_to_sell.append(t)

    if plan_action == "FLATTEN_TO_STABLES":
        print("[execute_dom_trade] FLATTEN_TO_STABLES: Selling all DOM tokens + BTC into USDC.")
        for token in tokens_to_sell:
            sell_all_to_usdc(token)
        print("[execute_dom_trade] End state should be 100% USDC (subject to dust).")
        return

    if plan_action == "SWITCH":
        # Step 1: Flatten everything to USDC
        print("[execute_dom_trade] SWITCH: First sell all DOM tokens + BTC to USDC.")
        for token in tokens_to_sell:
            sell_all_to_usdc(token)

        # Step 2: Buy the target token with all USDC, unless target is STABLES
        if target_type == "STABLES" or target_token == "NONE":
            print("[execute_dom_trade] Target is STABLES; no buy after flatten.")
            print("[execute_dom_trade] End state should be 100% USDC.")
            return

        if target_type in ("ALT", "BTC"):
            print(f"[execute_dom_trade] Now BUY {target_token} with ALL {STABLE_SYMBOL}.")
            buy_with_all_usdc(target_token)
            print(f"[execute_dom_trade] End state should be 100% {target_token} (subject to dust).")
            return

        print(f"[execute_dom_trade] Unknown target_type '{target_type}'. No trades executed.")
        return

    # Fallback for unknown plan_action
    print(f"[execute_dom_trade] Unknown plan_action '{plan_action}'. No trades executed.")


if __name__ == "__main__":
    main()
