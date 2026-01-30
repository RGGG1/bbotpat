#!/usr/bin/env python3
import os, time, json, math, hmac, hashlib, urllib.parse
from dataclasses import dataclass
from typing import Dict, Any
import requests

# ----------------- utilities -----------------

def utc():
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

def log(msg):
    print(f"[{utc()}] {msg}", flush=True)

def envs(k, d=""):
    v = os.getenv(k)
    return d if v is None else str(v)

def envf(k, d):
    try: return float(envs(k, d))
    except: return float(d)

def envi(k, d):
    try: return int(float(envs(k, d)))
    except: return int(d)

def safe_read_json(path, default):
    try:
        with open(path, "r", encoding="utf-8") as f:
            t = f.read().strip()
        return json.loads(t) if t else default
    except:
        return default

# ----------------- config -----------------

@dataclass
class Cfg:
    api_key: str
    api_secret: str
    base: str = "https://fapi.binance.com"
    poll: float = 2.0
    armed: bool = True
    dry: bool = False
    z_enter: float = 1.6
    z_exit: float = 0.2
    lev_min: int = 1
    lev_max: int = 20
    max_notional: float = 300.0
    min_notional: float = 10.0
    zmap_path: str = "kc3_zmap.json"

def load_cfg():
    return Cfg(
        api_key = envs("BINANCE_FAPI_KEY"),
        api_secret = envs("BINANCE_FAPI_SECRET"),
        poll = envf("KC3_POLL_SEC", 2.0),
        armed = envi("KC3_ARMED", 1) == 1,
        dry = envi("KC3_DRY_RUN", 0) == 1,
        z_enter = envf("KC3_Z_ENTER", 1.6),
        z_exit = envf("KC3_Z_EXIT", 0.2),
        max_notional = envf("KC3_MAX_NOTIONAL_USD", 300),
        min_notional = envf("KC3_MIN_NOTIONAL_USD", 10),
    )

# ----------------- binance api -----------------

def sign(secret, query):
    return hmac.new(secret.encode(), query.encode(), hashlib.sha256).hexdigest()

def api(C, method, path, params=None, signed=False):
    params = dict(params or {})
    headers = {"X-MBX-APIKEY": C.api_key}

    if signed:
        params["timestamp"] = int(time.time() * 1000)
        q = urllib.parse.urlencode(params)
        params["signature"] = sign(C.api_secret, q)

    url = C.base + path
    r = requests.request(method, url, params=params, headers=headers, timeout=10)
    r.raise_for_status()
    return r.json() if r.text else {}

# ----------------- trading helpers -----------------

def get_mark_price(C, symbol):
    j = api(C, "GET", "/fapi/v1/premiumIndex", {"symbol": symbol})
    return float(j["markPrice"])

def get_position_amt(C, symbol):
    j = api(C, "GET", "/fapi/v2/positionRisk", signed=True)
    for r in j:
        if r["symbol"] == symbol:
            return float(r["positionAmt"])
    return 0.0

def order_market(C, symbol, side, qty, reduce=False):
    if qty <= 0:
        return
    if C.dry:
        log(f"ORDER_DRY {symbol} {side} qty={qty}")
        return
    params = {
        "symbol": symbol,
        "side": side,
        "type": "MARKET",
        "quantity": qty,
    }
    if reduce:
        params["reduceOnly"] = "true"
    api(C, "POST", "/fapi/v1/order", params, signed=True)
    log(f"ORDER_OK {symbol} {side} qty={qty}")

# ----------------- main loop -----------------

def main():
    C = load_cfg()
    log(f"BOOT armed={int(C.armed)} dry_run={int(C.dry)} poll={C.poll}s")

    while True:
        try:
            zmap = safe_read_json(C.zmap_path, {})
            if not zmap:
                log("ZMAP_EMPTY")
                time.sleep(C.poll)
                continue

            symbol = zmap.get("symbol")
            z = float(zmap.get("z", 0))
            want = int(zmap.get("want", 0))

            if not symbol or abs(z) < C.z_enter:
                time.sleep(C.poll)
                continue

            pos = get_position_amt(C, symbol)
            price = get_mark_price(C, symbol)
            qty = C.max_notional / price

            if pos == 0:
                side = "BUY" if want > 0 else "SELL"
                log(f"OPEN {symbol} z={z:.4f} qty={qty}")
                order_market(C, symbol, side, qty)

            elif abs(z) <= C.z_exit:
                side = "SELL" if pos > 0 else "BUY"
                log(f"CLOSE {symbol}")
                order_market(C, symbol, side, abs(pos), reduce=True)

        except Exception as e:
            log(f"ERROR {type(e).__name__}: {e}")

        time.sleep(C.poll)

if __name__ == "__main__":
    main()
