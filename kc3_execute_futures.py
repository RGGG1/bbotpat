#!/usr/bin/env python3
"""
KC3 Futures Executor (USDⓈ-M) — SAFE SEQUENTIAL VERSION

Key safety rules:
- Will NOT trade until kc3_latest.json (or override) is *fresh after service start*.
- Live trading requires BOTH:
    LIVE_TRADING_KC3=1  AND  KC3_ARMED=1
  If KC3_ARMED!=1, it will log what it would do but never place orders.

- Flip flow is strictly:
    CLOSE -> wait flat -> refresh balance -> compute qty -> OPEN
  Never "close+open" in one combined action.

- On any live order error (4xx / Binance error codes), it aborts the cycle and
  enters a cooldown to prevent spam.

- Quantity is floored to step size and precision from exchangeInfo (fixes -1111).
  UNIUSDT has quantityPrecision=0 so orders must be whole integers.

- Writes trade history JSONL to:
    /var/www/bbotpat_live/kc3_trades.jsonl

State file:
  /root/bbotpat_live/data/kc3_futures_exec_state.json
"""

import os, json, time, hmac, hashlib, urllib.parse, math
from pathlib import Path
from datetime import datetime, timezone
import requests

VERSION = "2025-12-28-safe-sequential-v1"

# Paths
KC3_LATEST   = Path("/var/www/bbotpat_live/kc3_latest.json")
KC3_OVERRIDE = Path("/var/www/bbotpat_live/kc3_exec_desired.json")
STATE_PATH   = Path("/root/bbotpat_live/data/kc3_futures_exec_state.json")
TRADES_JSONL = Path("/var/www/bbotpat_live/kc3_trades.jsonl")

# Env
BASE_URL        = os.getenv("KC3_BASE_URL", "https://fapi.binance.com").strip()
SYMBOL_SUFFIX   = os.getenv("KC3_SYMBOL_SUFFIX", "USDT").strip()
QUOTE_ASSET     = os.getenv("KC3_QUOTE_ASSET", "BNFCR").strip()
LEV             = int(float(os.getenv("KC3_LEVERAGE", "5")))
MAX_NOTIONAL    = float(os.getenv("KC3_MAX_NOTIONAL", "0"))
POLL_SEC        = float(os.getenv("KC3_POLL_SEC", "5"))
SIGNAL_MAX_AGE  = float(os.getenv("KC3_SIGNAL_MAX_AGE_SEC", "600"))  # ignore signals older than this
MARGIN_BUFFER   = float(os.getenv("KC3_MARGIN_BUFFER", "0.70"))      # use only this fraction of available
COOLDOWN_SEC    = float(os.getenv("KC3_COOLDOWN_SEC", "60"))         # after error, wait this long
ARMED           = os.getenv("KC3_ARMED", "0").strip() == "1"

LIVE_TRADING_KC3 = os.getenv("LIVE_TRADING_KC3", "0").strip() == "1"
LIVE = (LIVE_TRADING_KC3 and ARMED)

API_KEY = os.getenv("BINANCE_API_KEY", "").strip()
API_SEC = os.getenv("BINANCE_API_SECRET", "").strip()

# Telegram (optional)
TG_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
TG_CHAT  = os.getenv("TELEGRAM_CHAT_ID", "").strip()

session = requests.Session()
session.headers.update({"User-Agent":"kc3-exec/1.0"})

def now_iso():
    return datetime.now(timezone.utc).isoformat().replace("+00:00","Z")

def log(msg):
    print(f"[{now_iso()}] {msg}", flush=True)

def tg_send(text):
    if not TG_TOKEN or not TG_CHAT:
        return
    try:
        url = f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage"
        session.post(url, json={"chat_id": TG_CHAT, "text": text}, timeout=10)
    except Exception as e:
        log(f"TG ERROR: {e}")

def load_json(path: Path):
    try:
        return json.loads(path.read_text())
    except Exception:
        return None

def save_state(st):
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = STATE_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(st, indent=2, sort_keys=True))
    tmp.replace(STATE_PATH)

def load_state():
    st = load_json(STATE_PATH)
    if not isinstance(st, dict):
        st = {}
    st.setdefault("cum_roi_frac", 0.0)
    st.setdefault("last_trade_roi_frac", 0.0)
    st.setdefault("last_signal_mtime", 0.0)
    st.setdefault("cooldown_until", 0.0)
    return st

def sign(params: dict) -> dict:
    qs = urllib.parse.urlencode(params)
    sig = hmac.new(API_SEC.encode(), qs.encode(), hashlib.sha256).hexdigest()
    params["signature"] = sig
    return params

def req(method, path, params=None):
    if not API_KEY or not API_SEC:
        raise RuntimeError("Missing BINANCE_API_KEY / BINANCE_API_SECRET")
    if params is None:
        params = {}
    params["timestamp"] = int(time.time() * 1000)
    sign(params)
    url = BASE_URL + path
    r = session.request(method, url, params=params, headers={"X-MBX-APIKEY": API_KEY}, timeout=15)
    return r

def get_exchange_rules(symbol: str):
    r = session.get(BASE_URL + "/fapi/v1/exchangeInfo", timeout=15)
    r.raise_for_status()
    data = r.json()
    sym = None
    for s in data.get("symbols", []):
        if s.get("symbol") == symbol:
            sym = s
            break
    if not sym:
        raise RuntimeError(f"Symbol not found in exchangeInfo: {symbol}")

    qty_prec = int(sym.get("quantityPrecision", 0))
    step = 1.0
    min_qty = 0.0
    for f in sym.get("filters", []):
        if f.get("filterType") in ("LOT_SIZE","MARKET_LOT_SIZE"):
            step = float(f.get("stepSize", "1"))
            min_qty = float(f.get("minQty", "0"))
    return {"qty_prec": qty_prec, "step": step, "min_qty": min_qty}

def floor_to_step(x: float, step: float, prec: int) -> float:
    if step <= 0:
        return round(x, prec)
    k = math.floor(x / step)
    y = k * step
    # apply precision
    if prec <= 0:
        return float(int(y))
    return float(f"{y:.{prec}f}")

def get_mark_price(symbol: str) -> float:
    r = session.get(BASE_URL + "/fapi/v1/premiumIndex", params={"symbol": symbol}, timeout=10)
    r.raise_for_status()
    j = r.json()
    return float(j["markPrice"])

def get_available_balance(asset: str) -> float:
    r = req("GET", "/fapi/v2/balance", {})
    if r.status_code != 200:
        raise RuntimeError(f"balance failed {r.status_code}: {r.text[:200]}")
    arr = r.json()
    for row in arr:
        if row.get("asset") == asset:
            return float(row.get("availableBalance", "0"))
    return 0.0

def get_position_amt(symbol: str) -> float:
    r = req("GET", "/fapi/v2/positionRisk", {})
    if r.status_code != 200:
        raise RuntimeError(f"positionRisk failed {r.status_code}: {r.text[:200]}")
    arr = r.json()
    for row in arr:
        if row.get("symbol") == symbol:
            return float(row.get("positionAmt", "0"))
    return 0.0

def set_leverage(symbol: str, lev: int):
    r = req("POST", "/fapi/v1/leverage", {"symbol": symbol, "leverage": lev})
    if r.status_code != 200:
        raise RuntimeError(f"set leverage failed {r.status_code}: {r.text[:200]}")
    return True

def place_market(symbol: str, side: str, qty: float, reduce_only: bool):
    p = {"symbol": symbol, "side": side, "type": "MARKET", "quantity": qty}
    if reduce_only:
        p["reduceOnly"] = "true"
    r = req("POST", "/fapi/v1/order", p)
    return r

def write_trade_jsonl(obj: dict):
    TRADES_JSONL.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(obj, separators=(",",":"), ensure_ascii=False)
    with TRADES_JSONL.open("a") as f:
        f.write(line + "\n")

def desired_from_files():
    src = None
    if KC3_OVERRIDE.exists():
        src = KC3_OVERRIDE
    elif KC3_LATEST.exists():
        src = KC3_LATEST
    else:
        return None, None

    j = load_json(src)
    if not isinstance(j, dict):
        return None, src

    pos = j.get("position") or {}
    side = (pos.get("side") or "").upper().strip()
    token = (pos.get("token") or "").upper().strip()
    if side not in ("LONG","SHORT","FLAT"):
        return None, src
    if side != "FLAT" and not token:
        return None, src
    return {"side": side, "token": token}, src

def main():
    service_start = time.time()
    log(f"KC3 Futures Executor starting. LIVE_TRADING_KC3={LIVE_TRADING_KC3} KC3_ARMED={'1' if ARMED else '0'} LIVE={LIVE} VERSION={VERSION}")
    log(f"BASE_URL={BASE_URL} QUOTE_ASSET={QUOTE_ASSET} SYMBOL_SUFFIX={SYMBOL_SUFFIX} LEV={LEV} MAX_NOTIONAL={MAX_NOTIONAL} POLL_SEC={POLL_SEC}")
    log(f"DESIRED override (authoritative if exists): {KC3_OVERRIDE} | fallback: {KC3_LATEST}")

    st = load_state()
    rules_cache = {}

    while True:
        try:
            st = load_state()

            # cooldown protection
            if time.time() < float(st.get("cooldown_until", 0.0)):
                time.sleep(POLL_SEC)
                continue

            desired, src = desired_from_files()
            if not desired:
                log("No desired position yet (no file or invalid JSON). Waiting...")
                time.sleep(POLL_SEC)
                continue

            # freshness gate: do not act on stale file after start
            mtime = src.stat().st_mtime
            age = time.time() - mtime
            if mtime < service_start:
                log("No fresh signal yet (signal file older than service start). Waiting...")
                time.sleep(POLL_SEC)
                continue
            if age > SIGNAL_MAX_AGE:
                log(f"Signal too old (age={age:.1f}s > {SIGNAL_MAX_AGE}s). Waiting...")
                time.sleep(POLL_SEC)
                continue

            # De-dupe: act only when signal file changed
            last_m = float(st.get("last_signal_mtime", 0.0))
            if mtime <= last_m:
                time.sleep(POLL_SEC)
                continue

            side = desired["side"]
            token = desired["token"]
            symbol = f"{token}{SYMBOL_SUFFIX}" if side != "FLAT" else None

            # If FLAT requested, we close any UNI*? We only support closing current symbol based on last open.
            if side == "FLAT":
                # Best effort: if state knows open_symbol, close it.
                open_symbol = st.get("open_symbol")
                if not open_symbol:
                    log("Desired FLAT but no open_symbol in state. Nothing to do.")
                    st["last_signal_mtime"] = mtime
                    save_state(st)
                    time.sleep(POLL_SEC)
                    continue
                symbol = open_symbol

            # load rules
            if symbol not in rules_cache:
                rules_cache[symbol] = get_exchange_rules(symbol)
                r = rules_cache[symbol]
                log(f"Rules {symbol}: step={r['step']} prec={r['qty_prec']} minQty={r['min_qty']}")

            rls = rules_cache[symbol]
            qty_prec = rls["qty_prec"]
            step = rls["step"]
            min_qty = rls["min_qty"]

            # set leverage once per symbol when we first see it
            try:
                set_leverage(symbol, LEV)
                log(f"Leverage set OK for {symbol} => {LEV}x")
            except Exception as e:
                log(f"Leverage set ERROR for {symbol}: {e}")
                st["cooldown_until"] = time.time() + COOLDOWN_SEC
                save_state(st)
                time.sleep(POLL_SEC)
                continue

            # determine current position
            pos_amt = get_position_amt(symbol)
            cur_side = "FLAT"
            if pos_amt > 0:
                cur_side = "LONG"
            elif pos_amt < 0:
                cur_side = "SHORT"

            desired_side = side if side != "FLAT" else "FLAT"

            # Only act when desired differs from current.
            if desired_side == cur_side:
                log(f"No action: desired={desired_side} equals current={cur_side} for {symbol}.")
                st["last_signal_mtime"] = mtime
                save_state(st)
                time.sleep(POLL_SEC)
                continue

            # ---- CLOSE if needed ----
            if cur_side != "FLAT":
                close_qty = abs(pos_amt)
                close_qty = floor_to_step(close_qty, step, qty_prec)
                if close_qty < min_qty:
                    log(f"CLOSE abort: computed close_qty {close_qty} < minQty {min_qty}.")
                    st["cooldown_until"] = time.time() + COOLDOWN_SEC
                    save_state(st)
                    time.sleep(POLL_SEC)
                    continue

                close_side = "SELL" if cur_side == "LONG" else "BUY"
                mark = get_mark_price(symbol)
                log(f"KC3 CLOSE {cur_side} {symbol} qty={close_qty} mark~{mark:.6g} LIVE={LIVE}")

                if LIVE:
                    resp = place_market(symbol, close_side, close_qty, reduce_only=True)
                    if resp.status_code != 200:
                        log(f"KC3 CLOSE ERROR: {resp.status_code} {resp.text[:500]}")
                        tg_send(f"KC3 CLOSE ERROR {symbol} {cur_side} qty={close_qty}: {resp.text[:200]}")
                        st["cooldown_until"] = time.time() + COOLDOWN_SEC
                        save_state(st)
                        time.sleep(POLL_SEC)
                        continue
                else:
                    log("(dry-run) close order skipped")

                # Wait until flat (hard requirement)
                ok = False
                for _ in range(30):  # up to ~15s
                    time.sleep(0.5)
                    pa = get_position_amt(symbol)
                    if abs(pa) < 1e-9:
                        ok = True
                        break
                if not ok:
                    log("KC3 ERROR: Close submitted but position not flat after timeout. ABORTING.")
                    tg_send(f"KC3 ERROR: {symbol} not flat after close timeout. Bot aborting cycle.")
                    st["cooldown_until"] = time.time() + COOLDOWN_SEC
                    save_state(st)
                    time.sleep(POLL_SEC)
                    continue

                # Optional: record exit ROI estimate if we had entry price
                entry = float(st.get("open_entry_price", 0.0) or 0.0)
                open_side = st.get("open_side")
                exit_price = mark
                roi = 0.0
                if entry > 0 and open_side in ("LONG","SHORT"):
                    if open_side == "LONG":
                        roi = (exit_price - entry) / entry
                    else:
                        roi = (entry - exit_price) / entry
                st["last_trade_roi_frac"] = roi
                st["cum_roi_frac"] = float(st.get("cum_roi_frac", 0.0)) + roi

                write_trade_jsonl({
                    "ts": now_iso(),
                    "symbol": symbol,
                    "side": open_side or cur_side,
                    "qty": close_qty,
                    "entry_price": entry if entry > 0 else None,
                    "exit_price": exit_price,
                    "roi_pct": round(roi * 100, 4),
                    "cum_roi_pct": round(float(st.get("cum_roi_frac",0.0)) * 100, 4),
                    "note": "CLOSE"
                })

                tg_send(f"KC3 CLOSE {symbol} {cur_side} qty={close_qty} exit~{exit_price:.6g} ROI={roi*100:.3f}% Cum={float(st.get('cum_roi_frac',0.0))*100:.3f}%")

                # Clear open state
                st["open_symbol"] = None
                st["open_side"] = None
                st["open_qty"] = None
                st["open_entry_price"] = None
                st["open_ts"] = None
                save_state(st)

                # Give Binance a moment to release margin
                time.sleep(1.0)

            # If desired is FLAT, stop after closing
            if desired_side == "FLAT":
                log("Desired FLAT achieved.")
                st["last_signal_mtime"] = mtime
                save_state(st)
                time.sleep(POLL_SEC)
                continue

            # ---- OPEN desired ----
            # Refresh balance after close
            avail = get_available_balance(QUOTE_ASSET)
            usable = math.floor(avail * MARGIN_BUFFER)  # whole units down
            if usable <= 0:
                log(f"KC3 OPEN abort: avail {QUOTE_ASSET}={avail:.8f} usable={usable} (buffer={MARGIN_BUFFER}).")
                st["cooldown_until"] = time.time() + COOLDOWN_SEC
                save_state(st)
                time.sleep(POLL_SEC)
                continue

            # apply max notional if set
            margin = float(usable)
            notional = margin * LEV
            if MAX_NOTIONAL and MAX_NOTIONAL > 0:
                notional = min(notional, MAX_NOTIONAL)
                # also ensure margin matches that notional
                margin = math.floor(notional / LEV)

            # compute qty from mark price
            mark = get_mark_price(symbol)
            raw_qty = notional / mark
            qty = floor_to_step(raw_qty, step, qty_prec)

            # enforce min qty
            if qty < min_qty:
                log(f"KC3 OPEN abort: qty {qty} < minQty {min_qty} (raw={raw_qty}).")
                st["cooldown_until"] = time.time() + COOLDOWN_SEC
                save_state(st)
                time.sleep(POLL_SEC)
                continue

            open_side = "BUY" if desired_side == "LONG" else "SELL"
            log(f"KC3 OPEN {desired_side} {symbol} margin~{margin:.0f} {QUOTE_ASSET} notional~{notional:.0f} qty~{qty} mark~{mark:.6g} LIVE={LIVE} (src={src})")

            if LIVE:
                resp = place_market(symbol, open_side, qty, reduce_only=False)
                if resp.status_code != 200:
                    log(f"KC3 OPEN ERROR: {resp.status_code} {resp.text[:500]}")
                    tg_send(f"KC3 OPEN ERROR {symbol} {desired_side} qty={qty}: {resp.text[:200]}")
                    # IMPORTANT: do NOT retry. Cooldown + wait for next fresh signal.
                    st["cooldown_until"] = time.time() + COOLDOWN_SEC
                    st["last_signal_mtime"] = mtime  # consume signal to avoid spam
                    save_state(st)
                    time.sleep(POLL_SEC)
                    continue
            else:
                log("(dry-run) open order skipped")

            # record open state
            st["open_symbol"] = symbol
            st["open_side"] = desired_side
            st["open_qty"] = qty
            st["open_entry_price"] = mark
            st["open_ts"] = now_iso()
            st["last_signal_mtime"] = mtime
            save_state(st)

            write_trade_jsonl({
                "ts": now_iso(),
                "symbol": symbol,
                "side": desired_side,
                "qty": qty,
                "entry_price": mark,
                "exit_price": None,
                "roi_pct": None,
                "cum_roi_pct": round(float(st.get("cum_roi_frac",0.0)) * 100, 4),
                "note": "OPEN"
            })

            tg_send(f"KC3 OPEN {symbol} {desired_side} qty={qty} entry~{mark:.6g} (armed={ARMED})")

        except Exception as e:
            log(f"KC3 FATAL LOOP ERROR: {e}")
            # prevent rapid spam on exceptions
            st = load_state()
            st["cooldown_until"] = time.time() + COOLDOWN_SEC
            save_state(st)

        time.sleep(POLL_SEC)

if __name__ == "__main__":
    main()
