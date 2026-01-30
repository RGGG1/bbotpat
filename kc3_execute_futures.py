import os, time, json, math, hmac, hashlib, urllib.parse
from dataclasses import dataclass
from typing import Dict, Any, Optional, Tuple, List
import requests
from collections import deque


def in_no_trade_window_utc() -> bool:
    # Default: block between 02:30 and 08:00 UTC (override with KC3_NO_TRADE_UTC="HH:MM-HH:MM"; set empty to disable)
    spec = envs("KC3_NO_TRADE_UTC", "02:30-08:00").strip()
    if not spec:
        return False
    try:
        a,b = spec.split("-",1)
        ah,am = [int(x) for x in a.split(":")]
        bh,bm = [int(x) for x in b.split(":")]
        now = time.gmtime()
        cur = now.tm_hour*60 + now.tm_min
        start = ah*60 + am
        end = bh*60 + bm
        if start <= end:
            return start <= cur < end
        # wraps midnight
        return cur >= start or cur < end
    except Exception:
        return False


def envs(k: str, default: str = "") -> str:
    v = os.getenv(k)
    return default if v is None else str(v)


def envf(k: str, default: float) -> float:
    try:
        return float(envs(k, str(default)))
    except Exception:
        return float(default)


def envi(k: str, default: int) -> int:
    try:
        return int(float(envs(k, str(default))))
    except Exception:
        return int(default)


def utc() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def log(msg: str) -> None:
    print(f"[{utc()}] {msg}", flush=True)


def safe_read_json(path: str, default: Any) -> Any:
    try:
        with open(path, "r", encoding="utf-8") as f:
            txt = f.read().strip()
        if not txt:
            return default
        return json.loads(txt)
    except Exception:
        return default


def safe_write_json(path: str, obj: Any) -> None:
    tmp = f"{path}.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, separators=(",", ":"))
    os.replace(tmp, path)


@dataclass
class Cfg:
    api_key: str
    api_secret: str
    base: str = "https://fapi.binance.com"
    poll_sec: float = 2.0
    armed: bool = True
    dry_run: bool = False

    z_enter: float = 1.6
    z_exit: float = 0.2

    lev_mode: str = "dynamic"  # "fixed" or "dynamic"
    lev_fixed: int = 10
    lev_min: int = 1
    lev_max: int = 20

    tp_mode: str = "vol"  # placeholder
    max_notional_usd: float = 300.0
    min_notional_usd: float = 10.0

    desired_path: str = "kc3_desired_position.json"
    zmap_path: str = "kc3_zmap.json"
    state_path: str = "kc3_exec_state.json"

    no_trade_utc: str = "02:30-08:00"

    # safety / loop behavior
    cooldown_sec: float = 8.0
    heartbeat_sec: float = 60.0


def load_cfg() -> Cfg:
    return Cfg(
        api_key=envs("BINANCE_FAPI_KEY", ""),
        api_secret=envs("BINANCE_FAPI_SECRET", ""),
        base=envs("BINANCE_FAPI_BASE", "https://fapi.binance.com"),
        poll_sec=envf("KC3_POLL_SEC", 2.0),
        armed=envi("KC3_ARMED", 1) == 1,
        dry_run=envi("KC3_DRY_RUN", 0) == 1,

        z_enter=envf("KC3_Z_ENTER", 1.6),
        z_exit=envf("KC3_Z_EXIT", 0.2),

        lev_mode=envs("KC3_LEV_MODE", "dynamic"),
        lev_fixed=envi("KC3_LEV_FIXED", 10),
        lev_min=envi("KC3_LEV_MIN", 1),
        lev_max=envi("KC3_LEV_MAX", 20),

        tp_mode=envs("KC3_TP_MODE", "vol"),
        max_notional_usd=envf("KC3_MAX_NOTIONAL_USD", 300.0),
        min_notional_usd=envf("KC3_MIN_NOTIONAL_USD", 10.0),

        desired_path=envs("KC3_DESIRED_PATH", "kc3_desired_position.json"),
        zmap_path=envs("KC3_ZMAP_PATH", "kc3_zmap.json"),
        state_path=envs("KC3_STATE_PATH", "kc3_exec_state.json"),

        no_trade_utc=envs("KC3_NO_TRADE_UTC", "02:30-08:00"),
        cooldown_sec=envf("KC3_COOLDOWN_SEC", 8.0),
        heartbeat_sec=envf("KC3_HEARTBEAT_SEC", 60.0),
    )


# -------------------------
# Binance API helpers
# -------------------------

def _sign(secret: str, query: str) -> str:
    return hmac.new(secret.encode("utf-8"), query.encode("utf-8"), hashlib.sha256).hexdigest()


def _req(
    C: Cfg,
    method: str,
    path: str,
    params: Optional[Dict[str, Any]] = None,
    signed: bool = False,
    timeout: float = 10.0,
) -> Any:
    params = dict(params or {})
    headers = {"X-MBX-APIKEY": C.api_key} if C.api_key else {}
    if signed:
        params["timestamp"] = int(time.time() * 1000)
        params["recvWindow"] = int(envf("KC3_RECV_WINDOW_MS", 5000))
        q = urllib.parse.urlencode(params, doseq=True)
        params["signature"] = _sign(C.api_secret, q)
    url = C.base.rstrip("/") + path
    try:
        if method.upper() == "GET":
            r = requests.get(url, params=params, headers=headers, timeout=timeout)
        elif method.upper() == "POST":
            r = requests.post(url, params=params, headers=headers, timeout=timeout)
        elif method.upper() == "DELETE":
            r = requests.delete(url, params=params, headers=headers, timeout=timeout)
        else:
            raise RuntimeError(f"bad method {method}")
        r.raise_for_status()
        if r.text:
            return r.json()
        return None
    except requests.HTTPError as e:
        body = ""
        try:
            body = r.text[:500]
        except Exception:
            pass
        raise RuntimeError(f"HTTPError {method} {path} status={getattr(r,'status_code',None)} body={body}") from e


# -------------------------
# Exchange info / filters
# -------------------------

class ExchangeInfoCache:
    def __init__(self):
        self._ts = 0.0
        self._data = None

    def get(self, C: Cfg, max_age_sec: float = 300.0) -> Dict[str, Any]:
        now = time.time()
        if self._data is None or (now - self._ts) > max_age_sec:
            self._data = _req(C, "GET", "/fapi/v1/exchangeInfo", signed=False, timeout=20)
            self._ts = now
        return self._data


XINFO = ExchangeInfoCache()


def get_symbol_filters(C: Cfg, symbol: str) -> Dict[str, Dict[str, str]]:
    info = XINFO.get(C)
    for s in info.get("symbols", []):
        if s.get("symbol") == symbol:
            out: Dict[str, Dict[str, str]] = {}
            for f in s.get("filters", []):
                out[f.get("filterType")] = f
            return out
    return {}


def floor_to_step(qty: float, step: float) -> float:
    if step <= 0:
        return qty
    return math.floor(qty / step) * step


def clamp(x: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, x))


def choose_leverage(C: Cfg, z_score: float) -> int:
    if C.lev_mode.lower() == "fixed":
        return int(clamp(C.lev_fixed, C.lev_min, C.lev_max))
    z = abs(float(z_score))
    # ramp between min and max as z goes from enter->enter*2
    a = C.z_enter
    b = max(a * 2.0, a + 1e-9)
    t = clamp((z - a) / (b - a), 0.0, 1.0)
    lev = C.lev_min + t * (C.lev_max - C.lev_min)
    return int(clamp(int(round(lev)), C.lev_min, C.lev_max))


def set_leverage(C: Cfg, symbol: str, leverage: int) -> None:
    if C.dry_run:
        log(f"SET_LEVERAGE_DRY symbol={symbol} leverage={leverage}")
        return
    _req(C, "POST", "/fapi/v1/leverage", params={"symbol": symbol, "leverage": int(leverage)}, signed=True)
    log(f"SET_LEVERAGE_OK symbol={symbol} leverage={int(leverage)}")


def get_mark_price(C: Cfg, symbol: str) -> float:
    j = _req(C, "GET", "/fapi/v1/premiumIndex", params={"symbol": symbol}, signed=False)
    return float(j.get("markPrice"))


def get_position_amt(C: Cfg, symbol: str) -> float:
    j = _req(C, "GET", "/fapi/v2/positionRisk", signed=True)
    for row in j:
        if row.get("symbol") == symbol:
            return float(row.get("positionAmt"))
    return 0.0


def cancel_all(C: Cfg, symbol: str) -> None:
    if C.dry_run:
        log(f"CANCEL_ALL_DRY symbol={symbol}")
        return
    try:
        _req(C, "DELETE", "/fapi/v1/allOpenOrders", params={"symbol": symbol}, signed=True)
        log(f"CANCEL_ALL_OK symbol={symbol}")
    except Exception as e:
        log(f"CANCEL_ALL_FAIL symbol={symbol} {e}")


def order_market(C: Cfg, symbol: str, side: str, qty: float, reduce_only: bool = False) -> Any:
    if qty <= 0:
        raise RuntimeError("qty<=0")
    params = {
        "symbol": symbol,
        "side": side,
        "type": "MARKET",
        "quantity": qty,
    }
    if reduce_only:
        params["reduceOnly"] = "true"
    if C.dry_run:
        log(f"ORDER_DRY symbol={symbol} side={side} qty={qty} reduceOnly={reduce_only}")
        return {"dry_run": True, "params": params}
    j = _req(C, "POST", "/fapi/v1/order", params=params, signed=True)
    return j


def normalize_qty(C: Cfg, symbol: str, qty_raw: float, price: float) -> float:
    f = get_symbol_filters(C, symbol)
    lot = f.get("LOT_SIZE") or {}
    step = float(lot.get("stepSize", "0.000001"))
    min_qty = float(lot.get("minQty", "0"))
    max_qty = float(lot.get("maxQty", "1e18"))
    # floor to step
    qty = floor_to_step(qty_raw, step)
    # clamp
    qty = clamp(qty, min_qty, max_qty)
    # min notional check (Binance futures often enforces minNotional)
    notion = qty * price
    if notion < C.min_notional_usd:
        # try bump to min_notional
        qty2 = C.min_notional_usd / max(price, 1e-9)
        qty2 = floor_to_step(qty2, step)
        qty2 = clamp(qty2, min_qty, max_qty)
        if qty2 * price >= C.min_notional_usd:
            qty = qty2
    return float(qty)


def desired_to_symbol(desired: Dict[str, Any]) -> Tuple[str, int, float]:
    """
    Returns (symbol, want, z_score)
      want: +1 long, -1 short, 0 flat
    """
    sym = str(desired.get("symbol") or desired.get("sym") or "")
    side = str(desired.get("side") or "").upper()
    want = int(desired.get("want") or desired.get("dir") or 0)
    z = float(desired.get("z_score") or desired.get("z") or 0.0)

    if not sym and isinstance(desired.get("best"), str):
        sym = desired["best"]

    if sym and not sym.endswith("USDT"):
        # normalize token->symbol convention
        sym = sym.upper() + "USDT"

    if want == 0:
        if side in ("BUY", "LONG"):
            want = 1
        elif side in ("SELL", "SHORT"):
            want = -1

    return sym.upper(), want, z


def calc_qty_for_notional(notional: float, price: float) -> float:
    if price <= 0:
        return 0.0
    return float(notional) / float(price)


def open_or_flip(C: Cfg, symbol: str, want: int, z_score: float, state: Dict[str, Any]) -> None:
    if want == 0:
        return

    # Set leverage first
    lev = choose_leverage(C, z_score)
    set_leverage(C, symbol, lev)

    # Determine notional to use (simple)
    notional = clamp(C.max_notional_usd, C.min_notional_usd, C.max_notional_usd)

    px = get_mark_price(C, symbol)
    qty_raw = calc_qty_for_notional(notional, px)
    qty = normalize_qty(C, symbol, qty_raw, px)

    if qty <= 0:
        raise RuntimeError(f"qty<=0 after normalize (raw={qty_raw} px={px})")

    # Determine side
    side = "BUY" if want > 0 else "SELL"

    log(f"OPEN symbol={symbol} want={want} z={z_score:.4f} lev={lev:.2f} px={px:.6f} qty={qty:.8f}")

    # Place
    j = order_market(C, symbol, side, qty, reduce_only=False)
    state.setdefault("last_order", {})[symbol] = {"t": utc(), "side": side, "qty": qty, "px": px, "z": z_score, "lev": lev}
    if isinstance(j, dict) and j.get("dry_run"):
        return


def close_position(C: Cfg, symbol: str, amt: float) -> None:
    if amt == 0.0:
        return
    side = "SELL" if amt > 0 else "BUY"
    qty = abs(float(amt))
    # normalize to lot size
    px = get_mark_price(C, symbol)
    qty = normalize_qty(C, symbol, qty, px)
    if qty <= 0:
        return
    log(f"CLOSE symbol={symbol} side={side} qty={qty:.8f}")
    cancel_all(C, symbol)
    order_market(C, symbol, side, qty, reduce_only=True)


def should_exit(z_score: float, want: int, current_amt: float, C: Cfg) -> bool:
    if current_amt == 0.0:
        return False
    cur_dir = 1 if current_amt > 0 else -1
    # if desired flips direction, exit
    if want != 0 and cur_dir != want:
        return True
    # if z is near mean, exit
    if abs(z_score) <= C.z_exit:
        return True
    return False


def main():
    C = load_cfg()
    log(f"BOOT armed={int(C.armed)} dry_run={int(C.dry_run)} poll={C.poll_sec:.1f}s tp_mode={C.tp_mode} lev_mode={C.lev_mode} z_enter={C.z_enter} z_exit={C.z_exit}")

    state_path = C.state_path
    state = safe_read_json(state_path, {})

    # heartbeat state
    hb_last = 0.0
    last_action = 0.0

    # guard: if no key, stay alive but never trade
    if not C.api_key or not C.api_secret:
        log("WARN missing BINANCE_FAPI_KEY/SECRET (will not trade)")

    while True:
        t0 = time.time()

        try:
            # HEARTBEAT
            if (t0 - hb_last) >= C.heartbeat_sec:
                hb_last = t0
                log(f"HEARTBEAT utc={time.strftime('%Y-%m-%d %H:%M:%S', time.gmtime())} armed={int(C.armed)} dry_run={int(C.dry_run)}")

            # no-trade window
            if in_no_trade_window_utc():
                # still keep process alive; do not trade
                time.sleep(max(0.2, C.poll_sec))
                continue

            # read desired + zmap
            desired = safe_read_json(C.desired_path, {})
            zmap = safe_read_json(C.zmap_path, {})

            if not zmap or zmap == {}:
                log("ZMAP_EMPTY (no signals to act on)")
                time.sleep(max(0.2, C.poll_sec))
                continue

            # pick top entry in zmap (expects {symbol: {...}} or {token:{...}})
            # Try a few shapes
            best_sym = None
            best = None

            # If zmap already looks like {"symbol":"BNBUSDT","want":...}
            if isinstance(zmap, dict) and "symbol" in zmap and ("want" in zmap or "side" in zmap):
                best = zmap
                best_sym, want, z = desired_to_symbol(best)
            else:
                # try dict of entries
                if isinstance(zmap, dict):
                    # choose by abs(z_score) if present
                    best_k = None
                    best_abs = -1.0
                    for k,v in zmap.items():
                        if isinstance(v, dict):
                            zz = float(v.get("z_score") or v.get("z") or 0.0)
                        else:
                            zz = 0.0
                        a = abs(zz)
                        if a > best_abs:
                            best_abs = a
                            best_k = k
                    if best_k is not None:
                        best = zmap.get(best_k)
                        if isinstance(best, dict):
                            # symbol might be best_k or inside
                            if not best.get("symbol") and isinstance(best_k, str):
                                best["symbol"] = best_k if best_k.endswith("USDT") else (best_k.upper()+"USDT")
                        else:
                            best = {"symbol": best_k}
                best_sym, want, z = desired_to_symbol(best or {})

            if not best_sym:
                log("ZMAP_EMPTY (no usable symbol)")
                time.sleep(max(0.2, C.poll_sec))
                continue

            # hysteresis enter check
            if want == 0:
                # If want missing, infer by z sign
                want = 1 if z > 0 else (-1 if z < 0 else 0)

            if abs(z) < C.z_enter:
                log(f"NO_ENTRY symbol={best_sym} z={z:.4f} (< z_enter {C.z_enter})")
                time.sleep(max(0.2, C.poll_sec))
                continue

            # cooldown to avoid spam
            if (t0 - last_action) < C.cooldown_sec:
                time.sleep(max(0.2, C.poll_sec))
                continue

            # do not trade if not armed or missing keys
            if (not C.armed) or (not C.api_key) or (not C.api_secret):
                log(f"SKIP armed={int(C.armed)} keys={int(bool(C.api_key and C.api_secret))} symbol={best_sym} want={want} z={z:.4f}")
                time.sleep(max(0.2, C.poll_sec))
                continue

            # read current position
            amt = get_position_amt(C, best_sym)

            # exit conditions
            if should_exit(z, want, amt, C):
                log(f"EXIT_COND symbol={best_sym} want={want} pos={amt} z={z:.4f}")
                close_position(C, best_sym, amt)
                last_action = time.time()
                time.sleep(max(0.2, C.poll_sec))
                continue

            # entry / flip
            if amt == 0.0:
                open_or_flip(C, best_sym, want, z, state)
                last_action = time.time()
            else:
                cur_dir = 1 if amt > 0 else -1
                if cur_dir != want:
                    # close then open
                    log(f"FLIP symbol={best_sym} from={cur_dir} to={want} pos={amt} z={z:.4f}")
                    close_position(C, best_sym, amt)
                    time.sleep(0.3)
                    open_or_flip(C, best_sym, want, z, state)
                    last_action = time.time()

            # persist state
            try:
                with open(state_path, "w", encoding="utf-8") as f:
                    json.dump(state, f, indent=2, sort_keys=True)
            except Exception:
                pass

        except Exception as e:
            log(f"LOOP_FAIL {type(e).__name__}: {e}")

        # sleep remaining
        dt = time.time() - t0
        time.sleep(max(0.05, C.poll_sec - dt))

if __name__ == "__main__":
    main()
