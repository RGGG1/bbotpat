#!/usr/bin/env python3
import json
import time
from pathlib import Path
from datetime import datetime, timezone

# Inputs (produced by live collector)
HMI_IN = Path("/var/www/bbotpat_live/hmi_latest.json")
PRICES_IN = Path("/var/www/bbotpat_live/prices_latest.json")

# State + outputs
STATE_DIR = Path("/root/bbotpat_live/data")
STATE_DIR.mkdir(parents=True, exist_ok=True)

STATE_PATH  = STATE_DIR / "kc3_state.json"
TRADES_PATH = Path("/var/www/bbotpat_live/kc3_trades.json")   # trade ledger (one row per trade)
LATEST_PATH = Path("/var/www/bbotpat_live/kc3_latest.json")   # current status snapshot

# Config
START_EQUITY_USD = 100.0   # ALWAYS $100 start
THRESH = 0.10              # trade when HMI changes by >= 0.10
LOOP_SEC = 1.0             # every second

EXCLUDE = {"BTC", "USDT", "USDC", "USDTC"}  # not tradable tokens
STABLE_HINTS = ("USD",)                    # treat any token containing USD as stable-ish

def utc_now_iso():
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")

def safe_read_json(path: Path):
    try:
        if not path.exists():
            return None
        return json.loads(path.read_text())
    except Exception:
        return None

def safe_write_json(path: Path, obj):
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(obj, indent=2))
    tmp.replace(path)

def ensure_trade_ledger():
    if not TRADES_PATH.exists():
        safe_write_json(TRADES_PATH, [])

def load_state():
    st = safe_read_json(STATE_PATH)
    if not isinstance(st, dict):
        st = {}
    st.setdefault("equity_usd", START_EQUITY_USD)   # realized equity
    st.setdefault("position", None)                # active position dict or None
    st.setdefault("last_hmi", None)
    st.setdefault("started_at", utc_now_iso())
    st.setdefault("trade_id_seq", 0)
    return st

def reset_all():
    # hard reset: $100, no position, empty ledger
    st = {
        "equity_usd": START_EQUITY_USD,
        "position": None,
        "last_hmi": None,
        "started_at": utc_now_iso(),
        "trade_id_seq": 0,
    }
    safe_write_json(STATE_PATH, st)
    safe_write_json(TRADES_PATH, [])
    safe_write_json(LATEST_PATH, {
        "timestamp": utc_now_iso(),
        "equity_usd": START_EQUITY_USD,
        "position": None,
        "roi_frac": 0.0,
        "total_equity_usd": START_EQUITY_USD,
        "hmi": None,
    })
    return st

def get_rows(prices_doc):
    rows = prices_doc.get("rows") if isinstance(prices_doc, dict) else None
    return rows if isinstance(rows, list) else None

def get_price(prices_doc, token: str):
    rows = get_rows(prices_doc)
    if not rows:
        return None
    token = token.upper()
    for r in rows:
        if str(r.get("token", "")).upper() == token:
            try:
                return float(r.get("price"))
            except Exception:
                return None
    return None

def pick_best_pot_roi_token(prices_doc):
    """
    Picks token with highest pot_roi_frac from prices_latest.json.
    Relies on live collector writing 'pot_roi_frac' per row.
    """
    rows = get_rows(prices_doc)
    if not rows:
        return None

    best_tok = None
    best_val = None

    for r in rows:
        tok = str(r.get("token", "")).upper()
        if not tok or tok in EXCLUDE:
            continue
        if any(h in tok for h in STABLE_HINTS):
            continue

        val = r.get("pot_roi_frac", None)
        if val is None:
            continue
        try:
            v = float(val)
        except Exception:
            continue

        if best_val is None or v > best_val:
            best_val = v
            best_tok = tok

    return best_tok

def trades_read():
    arr = safe_read_json(TRADES_PATH)
    return arr if isinstance(arr, list) else []

def trades_write(arr):
    safe_write_json(TRADES_PATH, arr)

def trade_open_record(st, hmi_now, side, token, entry_price):
    st["trade_id_seq"] = int(st.get("trade_id_seq", 0)) + 1
    trade_id = st["trade_id_seq"]

    rec = {
        "id": trade_id,
        "opened_at": utc_now_iso(),
        "closed_at": None,

        "token": token,
        "direction": side,          # LONG / SHORT
        "hmi_open": round(float(hmi_now), 4),

        "entry_price": float(entry_price),
        "exit_price": None,

        "roi_frac": None,           # filled on close
        "pnl_usd": None,            # filled on close
    }

    arr = trades_read()
    arr.append(rec)
    trades_write(arr)

    return trade_id

def trade_close_record(trade_id, exit_price, roi_frac, pnl_usd):
    arr = trades_read()
    for rec in reversed(arr):
        if rec.get("id") == trade_id:
            rec["closed_at"] = utc_now_iso()
            rec["exit_price"] = float(exit_price)
            rec["roi_frac"] = float(roi_frac)
            rec["pnl_usd"] = float(pnl_usd)
            break
    trades_write(arr)

def mark_to_market(st, prices_doc):
    eq = float(st["equity_usd"])
    pos = st.get("position")

    if not isinstance(pos, dict):
        total = eq
        return {"total_equity_usd": total, "roi_frac": (total / START_EQUITY_USD) - 1.0}

    token = pos["token"]
    side  = pos["side"]
    entry = float(pos["entry_price"])
    qty   = float(pos["qty"])

    px = get_price(prices_doc, token)
    if px is None:
        total = eq
        return {"total_equity_usd": total, "roi_frac": (total / START_EQUITY_USD) - 1.0}

    if side == "LONG":
        mtm = (px - entry) * qty
    else:  # SHORT
        mtm = (entry - px) * qty

    total = eq + mtm
    return {
        "total_equity_usd": total,
        "roi_frac": (total / START_EQUITY_USD) - 1.0,
        "current_price": px,
        "mtm_pnl_usd": mtm,
    }

def close_position(st, prices_doc):
    pos = st.get("position")
    if not isinstance(pos, dict):
        return st

    token = pos["token"]
    side  = pos["side"]
    entry = float(pos["entry_price"])
    qty   = float(pos["qty"])
    trade_id = pos.get("trade_id")

    px = get_price(prices_doc, token)
    if px is None:
        return st  # cannot close without a price

    if side == "LONG":
        pnl = (px - entry) * qty
    else:
        pnl = (entry - px) * qty

    # realized equity updates
    st["equity_usd"] = float(st["equity_usd"]) + pnl

    # trade ROI is on realized move vs entry (full-balance notional)
    # Notional = qty * entry = equity at open (because qty = equity/entry)
    notional = qty * entry if entry > 0 else 0.0
    roi_frac = (pnl / notional) if notional > 0 else 0.0

    if trade_id is not None:
        trade_close_record(trade_id, px, roi_frac, pnl)

    st["position"] = None
    return st

def open_position(st, prices_doc, side, token, hmi_now):
    px = get_price(prices_doc, token)
    if px is None or px <= 0:
        return st

    equity = float(st["equity_usd"])
    qty = equity / px  # full balance notional

    trade_id = trade_open_record(st, hmi_now, side, token, px)

    st["position"] = {
        "side": side,          # LONG / SHORT
        "token": token,
        "entry_price": px,
        "qty": qty,
        "opened_at": utc_now_iso(),
        "trade_id": trade_id,
    }
    return st

def decide(st, hmi_now, prices_doc):
    last = st.get("last_hmi")

    # first ever tick: just store, no trade
    if last is None:
        st["last_hmi"] = float(hmi_now)
        return st

    delta = float(hmi_now) - float(last)

    # no change big enough => do nothing, remain in whatever position you have
    if abs(delta) < THRESH:
        st["last_hmi"] = float(hmi_now)
        return st

    desired_side = "LONG" if delta > 0 else "SHORT"
    tok = pick_best_pot_roi_token(prices_doc)
    if not tok:
        st["last_hmi"] = float(hmi_now)
        return st

    pos = st.get("position")
    if isinstance(pos, dict):
        # If already correct token+direction, keep it
        if pos.get("side") == desired_side and str(pos.get("token", "")).upper() == tok:
            st["last_hmi"] = float(hmi_now)
            return st

        # otherwise close then reopen
        st = close_position(st, prices_doc)

    # open new position
    st = open_position(st, prices_doc, desired_side, tok, hmi_now)
    st["last_hmi"] = float(hmi_now)
    return st

def write_latest(st, hmi_now, prices_doc):
    mtm = mark_to_market(st, prices_doc)
    out = {
        "timestamp": utc_now_iso(),
        "hmi": float(hmi_now) if hmi_now is not None else None,
        "equity_usd": float(st["equity_usd"]),                # realized
        "total_equity_usd": float(mtm["total_equity_usd"]),   # mark-to-market
        "roi_frac": float(mtm["roi_frac"]),
        "position": st.get("position"),
    }
    safe_write_json(LATEST_PATH, out)

def main():
    ensure_trade_ledger()

    # If state is missing, start clean at $100
    if not STATE_PATH.exists():
        st = reset_all()
    else:
        st = load_state()

    while True:
        hmi_doc = safe_read_json(HMI_IN)
        prices_doc = safe_read_json(PRICES_IN)

        if isinstance(hmi_doc, dict) and isinstance(prices_doc, dict):
            try:
                hmi_now = float(hmi_doc.get("hmi"))
            except Exception:
                hmi_now = None

            if hmi_now is not None:
                st = decide(st, hmi_now, prices_doc)
                safe_write_json(STATE_PATH, st)
                write_latest(st, hmi_now, prices_doc)

        time.sleep(LOOP_SEC)

if __name__ == "__main__":
    main()
