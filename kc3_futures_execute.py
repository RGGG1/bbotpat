#!/usr/bin/env python3
"""
KC3 Futures Executor (Binance USD-M)
- Reads the desired position from /var/www/bbotpat_live/kc3_desired_position.json
- If LIVE_TRADING=1, it will:
    - close any opposite position (reduceOnly)
    - open the desired position (MARKET)
- If LIVE_TRADING=0, it will only log what it *would* do.

This is intentionally isolated from KC1/KC2.
"""

import os
import json
import time
import math
from pathlib import Path
from datetime import datetime, timezone

# -------- Paths --------
DESIRED_PATH = Path("/var/www/bbotpat_live/kc3_desired_position.json")
LOG_PATH = Path("/root/bbotpat_live/data/kc3_exec_log.jsonl")
STATE_PATH = Path("/root/bbotpat_live/data/kc3_exec_state.json")

# -------- Config --------
LOOP_SEC = 1.0
LIVE_TRADING = os.getenv("LIVE_TRADING", "0").strip() == "1"

BINANCE_KEY = os.getenv("BINANCE_API_KEY", "").strip()
BINANCE_SECRET = os.getenv("BINANCE_API_SECRET", "").strip()

# Notional sizing: use whatever KC3 agent says (usd_notional).
# Safety cap (optional)
MAX_NOTIONAL_USD = float(os.getenv("KC3_MAX_NOTIONAL_USD", "250").strip())

# -------- Binance client (python-binance) --------
try:
    from binance.client import Client
except Exception as e:
    raise SystemExit(
        "Missing dependency python-binance. Install in venv:\n"
        "  /root/bbotpat_live/.venv/bin/pip install python-binance\n"
        f"Import error: {e}"
    )

def utc_now_iso():
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")

def jread(path: Path):
    try:
        if not path.exists():
            return None
        return json.loads(path.read_text())
    except Exception:
        return None

def jwrite(path: Path, obj):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(obj, indent=2))
    tmp.replace(path)

def log_line(obj: dict):
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    obj = dict(obj)
    obj.setdefault("ts", utc_now_iso())
    LOG_PATH.write_text("", encoding="utf-8") if not LOG_PATH.exists() else None
    with LOG_PATH.open("a", encoding="utf-8") as f:
        f.write(json.dumps(obj) + "\n")

def load_state():
    st = jread(STATE_PATH)
    if not isinstance(st, dict):
        st = {}
    st.setdefault("last_signal_id", None)
    st.setdefault("last_symbol", None)
    st.setdefault("last_side", None)  # "LONG" / "SHORT"
    return st

def clamp_step(qty: float, step: float) -> float:
    if step <= 0:
        return qty
    return math.floor(qty / step) * step

def get_symbol_filters(client: Client, symbol: str):
    info = client.futures_exchange_info()
    for s in info.get("symbols", []):
        if s.get("symbol") == symbol:
            filters = {f["filterType"]: f for f in s.get("filters", [])}
            step = float(filters.get("LOT_SIZE", {}).get("stepSize", "1"))
            min_qty = float(filters.get("LOT_SIZE", {}).get("minQty", "0"))
            return step, min_qty
    return 1.0, 0.0

def futures_mark_price(client: Client, symbol: str) -> float:
    mp = client.futures_mark_price(symbol=symbol)
    return float(mp["markPrice"])

def futures_position_amt(client: Client, symbol: str) -> float:
    # positive = long, negative = short
    positions = client.futures_position_information(symbol=symbol)
    if not positions:
        return 0.0
    return float(positions[0].get("positionAmt", "0"))

def futures_close_position(client: Client, symbol: str):
    amt = futures_position_amt(client, symbol)
    if amt == 0:
        return {"closed": False, "reason": "no_position"}

    side = "SELL" if amt > 0 else "BUY"   # to reduce position to zero
    qty = abs(amt)

    if LIVE_TRADING:
        o = client.futures_create_order(
            symbol=symbol,
            side=side,
            type="MARKET",
            quantity=qty,
            reduceOnly=True,
        )
        return {"closed": True, "order": o}
    else:
        return {"closed": True, "paper": True, "would_side": side, "qty": qty}

def futures_open_position(client: Client, symbol: str, desired_side: str, usd_notional: float):
    # desired_side: "LONG" or "SHORT"
    px = futures_mark_price(client, symbol)
    usd_notional = float(usd_notional)

    if usd_notional <= 0:
        return {"opened": False, "reason": "notional<=0"}

    usd_notional = min(usd_notional, MAX_NOTIONAL_USD)

    step, min_qty = get_symbol_filters(client, symbol)
    raw_qty = usd_notional / px
    qty = clamp_step(raw_qty, step)

    if qty < min_qty or qty <= 0:
        return {"opened": False, "reason": "qty_too_small", "qty": qty, "min_qty": min_qty, "step": step, "px": px}

    side = "BUY" if desired_side == "LONG" else "SELL"

    if LIVE_TRADING:
        o = client.futures_create_order(
            symbol=symbol,
            side=side,
            type="MARKET",
            quantity=qty,
        )
        return {"opened": True, "order": o, "qty": qty, "px": px}
    else:
        return {"opened": True, "paper": True, "would_side": side, "qty": qty, "px": px}

def main():
    if LIVE_TRADING and (not BINANCE_KEY or not BINANCE_SECRET):
        raise SystemExit("LIVE_TRADING=1 but BINANCE_API_KEY/SECRET not set in environment.")

    client = Client(BINANCE_KEY, BINANCE_SECRET) if (BINANCE_KEY and BINANCE_SECRET) else Client()

    st = load_state()
    log_line({"msg": "kc3 executor started", "live_trading": LIVE_TRADING})

    while True:
        desired = jread(DESIRED_PATH)
        if not isinstance(desired, dict):
            time.sleep(LOOP_SEC)
            continue

        signal_id = desired.get("signal_id")
        token = str(desired.get("token", "")).upper()
        side = str(desired.get("side", "")).upper()       # LONG/SHORT
        usd_notional = desired.get("usd_notional", None)

        if not token or side not in {"LONG", "SHORT"} or usd_notional is None:
            time.sleep(LOOP_SEC)
            continue

        symbol = f"{token}USDT"

        # only act on new signals
        if signal_id is not None and signal_id == st.get("last_signal_id"):
            time.sleep(LOOP_SEC)
            continue

        # close any existing position on this symbol if it conflicts
        # (This keeps it simple: one-symbol-at-a-time execution controlled by KC3 agent)
        close_res = futures_close_position(client, symbol)

        open_res = futures_open_position(client, symbol, side, float(usd_notional))

        log_line({
            "event": "SIGNAL",
            "signal_id": signal_id,
            "symbol": symbol,
            "side": side,
            "usd_notional": usd_notional,
            "close": close_res,
            "open": open_res,
            "live_trading": LIVE_TRADING,
        })

        st["last_signal_id"] = signal_id
        st["last_symbol"] = symbol
        st["last_side"] = side
        jwrite(STATE_PATH, st)

        time.sleep(LOOP_SEC)

if __name__ == "__main__":
    main()
