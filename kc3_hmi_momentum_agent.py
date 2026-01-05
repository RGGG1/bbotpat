#!/usr/bin/env python3

import json
import time
from pathlib import Path
from datetime import datetime, timezone

HMI_PATH = Path("hmi_latest.json")
PRICES_PATH = Path("prices_latest.json")
DESIRED_PATH = Path("kc3_desired_position.json")

HMI_THRESHOLD = float(__import__("os").getenv("KC3_HMI_THRESHOLD", "0.5"))
POLL_SEC = int(float(__import__("os").getenv("KC3_AGENT_POLL_SEC", "5")))
LOG_EVERY_SEC = 60


def utc():
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def read_json(path: Path):
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text())
    except Exception:
        return None


def pick_best_token(prices_obj: dict):
    # Support both schemas:
    # - legacy/current: {"rows":[{"token":"BTC","pot_roi_pct":...}, ...]}
    # - newer: {"tokens":[{"token":"BTC","pot_roi_pct":...}, ...]}
    items = None
    if isinstance(prices_obj, dict):
        if isinstance(prices_obj.get("tokens"), list):
            items = prices_obj["tokens"]
        elif isinstance(prices_obj.get("rows"), list):
            items = prices_obj["rows"]

    if not items:
        return None

    # Choose by pot_roi_pct if present, else fallback to 24h change, else first row
    def score(x):
        try:
            if "pot_roi_pct" in x:
                return float(x.get("pot_roi_pct") or 0.0)
            if "change_24h" in x:
                return float(x.get("change_24h") or 0.0)
        except Exception:
            return 0.0
        return 0.0

    return max(items, key=score)


def main():
    last_anchor_hmi = None
    last_log = 0

    while True:
        now = time.time()
        try:
            hmi = read_json(HMI_PATH)
            prices = read_json(PRICES_PATH)

            if not hmi or "hmi" not in hmi:
                time.sleep(POLL_SEC)
                continue

            hmi_val = float(hmi["hmi"])

            if last_anchor_hmi is None:
                last_anchor_hmi = hmi_val

            # Log once per minute so you can see it’s alive + what it’s seeing
            if now - last_log >= LOG_EVERY_SEC:
                ts_prices = prices.get("timestamp") if isinstance(prices, dict) else None
                print(f"[{utc()}] AGENT alive | hmi={hmi_val} | prices_ts={ts_prices}", flush=True)
                last_log = now

            if not prices:
                time.sleep(POLL_SEC)
                continue

            best = pick_best_token(prices)
            if not best or "token" not in best:
                time.sleep(POLL_SEC)
                continue

            move = hmi_val - last_anchor_hmi
            if abs(move) < HMI_THRESHOLD:
                time.sleep(POLL_SEC)
                continue

            new_side = "LONG" if move > 0 else "SHORT"
            symbol = f"{best['token']}USDT"

            # Equity fallback: use 100 if not provided
            equity_usd = float(prices.get("equity_usd", 100.0)) if isinstance(prices, dict) else 100.0

            desired = {
                "side": new_side,
                "symbol": symbol,
                "notional_usd": equity_usd,
                "timestamp": utc(),
            }

            DESIRED_PATH.write_text(json.dumps(desired, indent=2) + "\n")
            print(f"[{utc()}] AGENT wrote desired: {desired}", flush=True)

            last_anchor_hmi = hmi_val
            time.sleep(POLL_SEC)

        except Exception as e:
            print(f"[{utc()}] AGENT error: {e}", flush=True)
            time.sleep(5)


if __name__ == "__main__":
    main()
