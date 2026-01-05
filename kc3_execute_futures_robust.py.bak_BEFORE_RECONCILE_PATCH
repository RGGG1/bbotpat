#!/usr/bin/env python3
import os, time, json, traceback
from pathlib import Path
from datetime import datetime, timezone
import kc3_execute_futures as base

STATUS = Path("/var/www/bbotpat_live/kc3_futures_status.json")

RECONCILE_SEC = float(os.getenv("KC3_RECONCILE_SEC", "30"))
HEARTBEAT_SEC = float(os.getenv("KC3_HEARTBEAT_SEC", "60"))

def utc():
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")

def write_status(obj):
    tmp = STATUS.with_suffix(".tmp")
    tmp.write_text(json.dumps(obj, indent=2) + "\n")
    tmp.replace(STATUS)

def normalize(sig):
    if not isinstance(sig, dict):
        return None
    if "token" not in sig and "best_token" in sig:
        sig["token"] = sig["best_token"]
    if "side" not in sig and "signal_side" in sig:
        sig["side"] = sig["signal_side"]
    sig.setdefault("src", "")
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
                raw = base.read_desired()
                sig = normalize(raw)
                if sig and sig.get("token") and sig.get("side"):
                    base.handle_signal(sig)
                    write_status({
                        "ts": utc(),
                        "alive": True,
                        "note": "reconciled",
                        "desired": sig
                    })
                last_reconcile = now

            time.sleep(1)

        except Exception:
            traceback.print_exc()
            time.sleep(5)

if __name__ == "__main__":
    main()
