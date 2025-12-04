import os
import json
import time
from typing import Any, Dict

from db_utils import init_db, log_hmi, log_price_row, log_kc1, log_kc2

BASE_DIR = os.path.dirname(__file__)


def load_json(name: str) -> Any:
    path = os.path.join(BASE_DIR, name)
    if not os.path.exists(path):
        return None
    try:
        with open(path, "r") as f:
            return json.load(f)
    except Exception as e:
        print(f"[log_to_db] Error loading {name}: {e}")
        return None


def log_hmi_from_file():
    data = load_json("hmi_latest.json")
    if not data:
        return
    hmi = data.get("hmi")
    band = data.get("band")
    ts = data.get("timestamp")
    if hmi is None:
        return
    try:
        log_hmi(float(hmi), band, ts=ts)
        print("[log_to_db] Logged HMI")
    except Exception as e:
        print(f"[log_to_db] Error logging HMI: {e}")


def log_prices_from_file():
    data = load_json("prices_latest.json")
    if not data:
        return
    ts = data.get("timestamp", int(time.time()))
    rows = data.get("rows", [])
    for row in rows:
        try:
            log_price_row(ts, row)
        except Exception as e:
            print(f"[log_to_db] Error logging price row {row.get('token')}: {e}")
    print(f"[log_to_db] Logged {len(rows)} price rows")


def log_kc1_from_file():
    data = load_json("knifecatcher_latest.json")
    if not data:
        return
    base = data.get("base_balance_usd")
    port = data.get("portfolio_value")
    btc_val = data.get("btc_value")
    # No explicit timestamp in file; use "now"
    try:
        log_kc1(base, port, btc_val, ts=None)
        print("[log_to_db] Logged KC1 snapshot")
    except Exception as e:
        print(f"[log_to_db] Error logging KC1: {e}")


def log_kc2_from_file():
    data = load_json("dom_signals_hourly.json")
    if not data:
        return

    ts = data.get("timestamp")
    equity = data.get("equity_usd")
    roi_frac = data.get("roi_frac")
    pos = data.get("position") or {}
    token = pos.get("token")
    entry_price = pos.get("entry_price")
    target_price = pos.get("target_price")
    hmi_override = bool(pos.get("hmi_override", False))
    benchmarks = data.get("benchmarks") or {}

    try:
        log_kc2(
            equity_usd=equity,
            roi_frac=roi_frac,
            token=token,
            entry_price=entry_price,
            target_price=target_price,
            hmi_override=hmi_override,
            benchmarks=benchmarks,
            ts=ts,
        )
        print("[log_to_db] Logged KC2 snapshot")
    except Exception as e:
        print(f"[log_to_db] Error logging KC2: {e}")


def main():
    init_db()
    log_hmi_from_file()
    log_prices_from_file()
    log_kc1_from_file()
    log_kc2_from_file()


if __name__ == "__main__":
    main()
