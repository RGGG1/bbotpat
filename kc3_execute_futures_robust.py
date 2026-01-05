#!/usr/bin/env python3
import os, time, json, traceback
from pathlib import Path
from datetime import datetime, timezone
import kc3_execute_futures as base

STATUS = Path("/var/www/bbotpat_live/kc3_futures_status.json")
DESIRED = Path("/root/bbotpat_live/kc3_desired_position.json")

RECONCILE_SEC = float(os.getenv("KC3_RECONCILE_SEC", "30"))
HEARTBEAT_SEC = float(os.getenv("KC3_HEARTBEAT_SEC", "60"))

def utc():
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")

def write_status(obj):
    tmp = STATUS.with_suffix(".tmp")
    tmp.write_text(json.dumps(obj, indent=2) + "\n")
    tmp.replace(STATUS)

def read_desired():
    if not DESIRED.exists():
        return None
    try:
        raw = json.loads(DESIRED.read_text() or "{}")
    except Exception:
        return None
    if not isinstance(raw, dict):
        return None

    # Normalize
    if "token" not in raw and "best_token" in raw:
        raw["token"] = raw["best_token"]
    if "symbol" in raw and "token" not in raw:
        sym = str(raw["symbol"]).upper().strip()
        raw["token"] = sym[:-4] if sym.endswith("USDT") else sym
    if "side" not in raw and "signal_side" in raw:
        raw["side"] = raw["signal_side"]

    sig = {
        "token": raw.get("token"),
        "side": raw.get("side"),
        "symbol": raw.get("symbol", (str(raw.get("token", "")).upper() + "USDT") if raw.get("token") else None),
        "notional_usd": float(raw.get("notional_usd", raw.get("usd", 100.0)) or 100.0),
        "src": raw.get("src", "robust"),
        "ts": raw.get("timestamp", raw.get("ts", utc())),
    }

    if not sig.get("token") or not sig.get("side"):
        return None
    return sig

def main():
    base.log("ROBUST wrapper started")
    last_reconcile = 0
    last_beat = 0

    while True:
        try:
            now = time.time()

            if now - last_beat >= HEARTBEAT_SEC:
                write_status({"ts": utc(), "alive": True, "note": "heartbeat"})
                base.log("HEARTBEAT: alive")
                last_beat = now

            if now - last_reconcile >= RECONCILE_SEC:
                sig = read_desired()
                if not sig:
                    write_status({"ts": utc(), "alive": True, "note": "no_desired"})
                    last_reconcile = now
                    time.sleep(1)
                    continue

                # IMPORTANT: use base's internal execution path
                base.handle_signal(sig)

                write_status({"ts": utc(), "alive": True, "note": "reconciled", "desired": sig})
                last_reconcile = now

            time.sleep(1)

        except Exception:
            traceback.print_exc()
            write_status({"ts": utc(), "alive": True, "note": "error", "err": "see log"})
            time.sleep(5)

if __name__ == "__main__":
    main()
