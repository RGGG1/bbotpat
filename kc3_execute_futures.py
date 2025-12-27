#!/usr/bin/env python3
"""
KC3 Futures Executor (USDⓈ-M)

Fixes:
- Loads /root/bbotpat_live/.env itself (systemd Environment was empty on your host).
- Manual override file is authoritative when present:
    /var/www/bbotpat_live/kc3_exec_desired.json
  Fallback:
    /var/www/bbotpat_live/kc3_latest.json
- Adds MIN_HOLD_SEC to prevent rapid flip-churn (default 60s when LIVE, 0 when dry-run).
- Close-then-open flip flow (reduceOnly close, then open).
- Floors notional down to whole units (as requested), and floors qty to stepSize from exchangeInfo (fixes -1111).
- Uses QUOTE_ASSET for balance (e.g. BNFCR) but trades SYMBOL_SUFFIX market (e.g. UNIUSDT).

Telegram:
- Sends token, direction, entry price, last trade ROI, cumulative ROI.

State:
  /root/bbotpat_live/data/kc3_futures_exec_state.json
"""

import os, json, time, hmac, hashlib, urllib.parse, math
from pathlib import Path
from datetime import datetime, timezone
import requests

# -------- paths --------
ENV_PATH      = Path("/root/bbotpat_live/.env")
STATE_PATH    = Path("/root/bbotpat_live/data/kc3_futures_exec_state.json")
STATE_PATH.parent.mkdir(parents=True, exist_ok=True)

DESIRED_OVERRIDE = Path("/var/www/bbotpat_live/kc3_exec_desired.json")
DESIRED_LATEST   = Path("/var/www/bbotpat_live/kc3_latest.json")

# -------- util --------
def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")

def log(msg: str):
    print(f"[{now_iso()}] {msg}", flush=True)

def load_env_file(path: Path):
    if not path.exists():
        return
    for raw in path.read_text().splitlines():
        s = raw.strip()
        if not s or s.startswith("#"):
            continue
        if "=" not in s:
            continue
        k, v = s.split("=", 1)
        k = k.strip()
        v = v.strip().strip('"').strip("'")
        os.environ.setdefault(k, v)

load_env_file(ENV_PATH)

def env_bool(k: str, default: bool=False) -> bool:
    v = os.getenv(k)
    if v is None:
        return default
    return str(v).strip().lower() in ("1", "true", "yes", "y", "on")

def env_float(k: str, default: float=0.0) -> float:
    v = os.getenv(k)
    if v is None or v == "":
        return default
    try:
        return float(v)
    except:
        return default

def env_int(k: str, default: int=0) -> int:
    v = os.getenv(k)
    if v is None or v == "":
        return default
    try:
        return int(float(v))
    except:
        return default

# -------- config --------
BASE_URL     = os.getenv("BINANCE_FAPI_BASE_URL", "https://fapi.binance.com").strip()
API_KEY      = os.getenv("BINANCE_API_KEY", "").strip()
API_SECRET   = os.getenv("BINANCE_API_SECRET", "").strip()

LIVE_TRADING = env_bool("LIVE_TRADING_KC3", False)
LEVERAGE     = env_int("LEVERAGE", 5)
SYMBOL_SUFFIX= os.getenv("SYMBOL_SUFFIX", "USDT").strip().upper()   # we trade UNIUSDT etc
QUOTE_ASSET  = os.getenv("QUOTE_ASSET", "BNFCR").strip().upper()    # balance asset for EU
POLL_SEC     = env_float("POLL_SEC", 2.0)

# Hold-time: prevents fee-churn on noisy signals
MIN_HOLD_SEC = env_int("MIN_HOLD_SEC", 60 if LIVE_TRADING else 0)

# optional cap
MAX_NOTIONAL = env_float("MAX_NOTIONAL", 0.0)

# Telegram (same envs as your other bots)
TG_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
TG_CHAT_ID   = os.getenv("TELEGRAM_CHAT_ID", "").strip()

# -------- Binance signed req --------
SESSION = requests.Session()
SESSION.headers.update({"User-Agent": "kc3-futures-exec/1.0"})

def require_creds():
    if not API_KEY or not API_SECRET:
        raise RuntimeError("Missing BINANCE_API_KEY / BINANCE_API_SECRET in .env")

def sign_params(params: dict) -> dict:
    qs = urllib.parse.urlencode(params, doseq=True)
    sig = hmac.new(API_SECRET.encode(), qs.encode(), hashlib.sha256).hexdigest()
    params["signature"] = sig
    return params

def bget(path: str, params=None, signed=False):
    params = params or {}
    if signed:
        params["timestamp"] = int(time.time() * 1000)
        sign_params(params)
        headers = {"X-MBX-APIKEY": API_KEY}
    else:
        headers = {}
    r = SESSION.get(BASE_URL + path, params=params, headers=headers, timeout=10)
    if r.status_code != 200:
        raise RuntimeError(f"GET {path} failed {r.status_code}: {r.text}")
    return r.json()

def bpost(path: str, params=None, signed=True):
    params = params or {}
    if signed:
        params["timestamp"] = int(time.time() * 1000)
        sign_params(params)
        headers = {"X-MBX-APIKEY": API_KEY}
    else:
        headers = {}
    r = SESSION.post(BASE_URL + path, params=params, headers=headers, timeout=10)
    if r.status_code != 200:
        raise RuntimeError(f"POST {path} failed {r.status_code}: {r.text}")
    return r.json()

# -------- market info / precision --------
_EXINFO = None
_SYMBOL_FILTERS = {}

def exchange_info():
    global _EXINFO
    if _EXINFO is None:
        _EXINFO = bget("/fapi/v1/exchangeInfo")
    return _EXINFO

def load_symbol_filters(symbol: str):
    if symbol in _SYMBOL_FILTERS:
        return _SYMBOL_FILTERS[symbol]
    info = exchange_info()
    for s in info.get("symbols", []):
        if s.get("symbol") == symbol:
            filters = {f.get("filterType"): f for f in s.get("filters", [])}
            lot = filters.get("LOT_SIZE", {})
            step = float(lot.get("stepSize", "1"))
            min_qty = float(lot.get("minQty", "0"))
            _SYMBOL_FILTERS[symbol] = {"stepSize": step, "minQty": min_qty}
            return _SYMBOL_FILTERS[symbol]
    raise RuntimeError(f"Symbol {symbol} not found in exchangeInfo")

def floor_to_step(qty: float, step: float) -> float:
    if step <= 0:
        return qty
    n = math.floor(qty / step + 1e-12)
    return n * step

# -------- core endpoints --------
def mark_price(symbol: str) -> float:
    j = bget("/fapi/v1/premiumIndex", params={"symbol": symbol})
    return float(j.get("markPrice", 0.0))

def position_risk():
    return bget("/fapi/v2/positionRisk", signed=True)

def get_position_amt(symbol: str) -> float:
    pr = position_risk()
    if isinstance(pr, list):
        for row in pr:
            if row.get("symbol") == symbol:
                try:
                    return float(row.get("positionAmt", 0.0))
                except:
                    return 0.0
    return 0.0

def get_position_entry_price(symbol: str) -> float:
    pr = position_risk()
    if isinstance(pr, list):
        for row in pr:
            if row.get("symbol") == symbol:
                try:
                    return float(row.get("entryPrice", 0.0))
                except:
                    return 0.0
    return 0.0

def get_available_quote_balance() -> float:
    # your own check proved /fapi/v2/balance shows BNFCR availableBalance
    bal = bget("/fapi/v2/balance", signed=True)
    if isinstance(bal, list):
        for row in bal:
            if str(row.get("asset", "")).upper() == QUOTE_ASSET:
                for k in ("availableBalance", "balance"):
                    v = row.get(k)
                    if v is not None:
                        try:
                            return float(v)
                        except:
                            pass
    return 0.0

def set_leverage(symbol: str, lev: int):
    bpost("/fapi/v1/leverage", params={"symbol": symbol, "leverage": lev}, signed=True)

def place_market_order(symbol: str, side: str, qty: float, reduce_only: bool):
    params = {
        "symbol": symbol,
        "side": side,              # BUY / SELL
        "type": "MARKET",
        "quantity": f"{qty:.16f}".rstrip("0").rstrip("."),
        "reduceOnly": "true" if reduce_only else "false",
        "newOrderRespType": "RESULT",
    }
    return bpost("/fapi/v1/order", params=params, signed=True)

# -------- desired state --------
def read_json(path: Path):
    try:
        if not path.exists():
            return None
        return json.loads(path.read_text())
    except:
        return None

def read_desired():
    # override is authoritative when file exists
    if DESIRED_OVERRIDE.exists():
        j = read_json(DESIRED_OVERRIDE)
        if isinstance(j, dict) and isinstance(j.get("position"), dict):
            return j, str(DESIRED_OVERRIDE)
    j = read_json(DESIRED_LATEST)
    if isinstance(j, dict) and isinstance(j.get("position"), dict):
        return j, str(DESIRED_LATEST)
    return None, None

def symbol_from_token(token: str) -> str:
    return f"{token.upper()}{SYMBOL_SUFFIX}"

# -------- telegram --------
def tg_send(text: str):
    if not TG_BOT_TOKEN or not TG_CHAT_ID:
        return
    try:
        url = f"https://api.telegram.org/bot{TG_BOT_TOKEN}/sendMessage"
        SESSION.post(url, data={"chat_id": TG_CHAT_ID, "text": text}, timeout=10)
    except:
        pass

# -------- state --------
def load_state() -> dict:
    if STATE_PATH.exists():
        try:
            return json.loads(STATE_PATH.read_text())
        except:
            pass
    return {
        "cum_roi_frac": 0.0,
        "last_trade_roi_frac": 0.0,
        "open_symbol": None,
        "open_token": None,
        "open_side": None,  # "LONG"/"SHORT"
        "open_qty": 0.0,
        "open_entry_price": 0.0,
        "open_ts": None,
        "last_action_key": None,
        "last_trade_ts": None,
    }

def save_state(st: dict):
    STATE_PATH.write_text(json.dumps(st, indent=2, sort_keys=True))

def roi_frac(entry: float, exit_: float, side: str, lev: int) -> float:
    if entry <= 0 or exit_ <= 0:
        return 0.0
    if side == "LONG":
        return ((exit_ - entry) / entry) * lev
    else:
        return ((entry - exit_) / entry) * lev

# -------- main loop --------
def main():
    require_creds()
    st = load_state()

    log(f"KC3 Futures Executor starting. LIVE_TRADING_KC3={LIVE_TRADING}")
    log(f"BASE_URL={BASE_URL} QUOTE_ASSET={QUOTE_ASSET} SYMBOL_SUFFIX={SYMBOL_SUFFIX} LEV={LEVERAGE} MAX_NOTIONAL={MAX_NOTIONAL}")
    log(f"DESIRED override (authoritative if exists): {DESIRED_OVERRIDE} | fallback: {DESIRED_LATEST}")
    log(f"MIN_HOLD_SEC={MIN_HOLD_SEC} POLL_SEC={POLL_SEC}")

    last_leverage_symbol = None

    while True:
        try:
            desired, src = read_desired()
            if not desired:
                time.sleep(POLL_SEC)
                continue

            pos = desired.get("position", {})
            token = str(pos.get("token", "")).upper().strip()
            want_side = str(pos.get("side", "")).upper().strip()  # LONG/SHORT

            if not token or want_side not in ("LONG", "SHORT"):
                time.sleep(POLL_SEC)
                continue

            symbol = symbol_from_token(token)

            # set leverage once per symbol
            if symbol != last_leverage_symbol:
                try:
                    set_leverage(symbol, LEVERAGE)
                    log(f"Leverage set OK for {symbol} => {LEVERAGE}x")
                    last_leverage_symbol = symbol
                except Exception as e:
                    log(f"Warn: leverage set failed for {symbol}: {e}")

            # enforce hold time (prevents flip-churn)
            now = time.time()
            if MIN_HOLD_SEC > 0 and st.get("last_trade_ts") is not None:
                age = now - float(st["last_trade_ts"])
                if age < MIN_HOLD_SEC:
                    # do nothing until hold expires
                    time.sleep(POLL_SEC)
                    continue

            # check current position
            cur_amt = get_position_amt(symbol)  # + long, - short
            have_side = None
            have_qty = 0.0
            if abs(cur_amt) > 1e-12:
                have_side = "LONG" if cur_amt > 0 else "SHORT"
                have_qty = abs(cur_amt)

            # If already aligned, do nothing
            if have_side == want_side:
                time.sleep(POLL_SEC)
                continue

            # Close existing position if any (close-then-open)
            if have_side is not None and have_qty > 0:
                exit_est = mark_price(symbol)
                entry = get_position_entry_price(symbol) or st.get("open_entry_price", 0.0)
                r = roi_frac(entry, exit_est, have_side, LEVERAGE)
                st["last_trade_roi_frac"] = r
                st["cum_roi_frac"] = float(st.get("cum_roi_frac", 0.0)) + float(r)

                close_side = "SELL" if have_side == "LONG" else "BUY"
                msg = (f"KC3 flip: CLOSE {have_side} {symbol} qty={have_qty} "
                       f"(est exit={exit_est:.6f} roi={r*100:.3f}%) then OPEN {want_side} (LIVE={LIVE_TRADING})")
                log(msg)

                if LIVE_TRADING:
                    place_market_order(symbol, close_side, have_qty, reduce_only=True)
                    time.sleep(0.4)  # tiny delay to let the position update

                    # ensure flat (best-effort)
                    cur2 = get_position_amt(symbol)
                    if abs(cur2) > 1e-12:
                        close2_side = "SELL" if cur2 > 0 else "BUY"
                        place_market_order(symbol, close2_side, abs(cur2), reduce_only=True)
                        time.sleep(0.2)

                tg_send(f"{msg}\nPrev ROI: {st['last_trade_roi_frac']*100:.3f}% | Cum ROI: {st['cum_roi_frac']*100:.3f}%")

            # size new position from QUOTE_ASSET balance (floor to whole units)
            avail = get_available_quote_balance()
            notional = math.floor(avail)  # <-- whole units only
            if MAX_NOTIONAL > 0:
                notional = min(notional, int(MAX_NOTIONAL))

            if notional <= 0:
                log(f"KC3 ERROR: Not enough available {QUOTE_ASSET} to open (avail={avail}).")
                time.sleep(POLL_SEC)
                continue

            px = mark_price(symbol)
            if px <= 0:
                raise RuntimeError(f"Bad mark price for {symbol}: {px}")

            raw_qty = notional / px
            filt = load_symbol_filters(symbol)
            step = float(filt["stepSize"])
            min_qty = float(filt["minQty"])

            qty = floor_to_step(raw_qty, step)
            if qty < min_qty:
                log(f"KC3 ERROR: qty too small after step floor: raw={raw_qty} step={step} => {qty}, minQty={min_qty}")
                time.sleep(POLL_SEC)
                continue

            order_side = "BUY" if want_side == "LONG" else "SELL"

            log(f"KC3 open: {want_side} {symbol} notional~{notional} {QUOTE_ASSET} qty~{qty} LIVE={LIVE_TRADING} (src={src})")

            if LIVE_TRADING:
                place_market_order(symbol, order_side, qty, reduce_only=False)
                time.sleep(0.5)
                entry_px = get_position_entry_price(symbol) or px
            else:
                entry_px = px

            # update state
            st["open_symbol"] = symbol
            st["open_token"] = token
            st["open_side"] = want_side
            st["open_qty"] = float(qty)
            st["open_entry_price"] = float(entry_px)
            st["open_ts"] = now_iso()
            st["last_action_key"] = f"{symbol}:{want_side}"
            st["last_trade_ts"] = now
            save_state(st)

            tg_send(
                f"KC3 OPEN {want_side} {token} ({symbol})\n"
                f"Entry: {entry_px:.6f}\n"
                f"Prev ROI: {st['last_trade_roi_frac']*100:.3f}%\n"
                f"Cum ROI: {st['cum_roi_frac']*100:.3f}%"
            )

        except Exception as e:
            log(f"KC3 ERROR: {e}")

        time.sleep(POLL_SEC)

if __name__ == "__main__":
    main()
