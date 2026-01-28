import os, time, json, math, hmac, hashlib, urllib.parse
from dataclasses import dataclass
from typing import Dict, Any, Optional, Tuple, List
import requests
from collections import deque

def envf(k, d): 
    try: return float(os.getenv(k, d))
    except: return float(d)
def envi(k, d):
    try: return int(float(os.getenv(k, d)))
    except: return int(d)
def envb(k, d):
    v=str(os.getenv(k, "")).strip().lower()
    if v=="":
        return bool(d)
    return v in ("1","true","yes","y","on")
def envs(k, d=""):
    v=os.getenv(k, None)
    return d if v is None else v

def log(msg: str):
    ts=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    print(f"[{ts}] {msg}", flush=True)

@dataclass
class Cfg:
    base_url: str
    api_key: str
    api_secret: str

    poll_sec: float
    armed: bool
    dry_run: bool

    quote_asset: str
    symbol_suffix: str
    usd_notional: float

    # signal plumbing
    zmap_path: str
    lag_z_enter: float
    lag_z_exit: float
    lag_mode: str  # both/long/short

    # leverage
    lev_mode: str
    lev_min: float
    lev_base: float
    lev_max: float
    lev_z_full: float

    # dynamic TP
    tp_mode: str           # fixed / vol
    tp_pct_fixed: float
    tp_check_sec: float
    tp_vol_lookback_sec: float
    tp_k: float
    tp_min: float
    tp_max: float

    # exits
    roll_tp_drop: float
    edge_stop_enabled: bool
    edge_stop_lev_dd: float

    # misc
    min_hold_sec: float
    wait_after_close_sec: float

def load_cfg() -> Cfg:
    return Cfg(
        base_url=envs("BINANCE_FUTURES_BASE_URL","https://fapi.binance.com").rstrip("/"),
        api_key=envs("BINANCE_FAPI_KEY", envs("BINANCE_API_KEY","")),
        api_secret=envs("BINANCE_FAPI_SECRET", envs("BINANCE_API_SECRET","")),

        poll_sec=envf("KC3_POLL_SEC", 2.0),
        armed=envb("KC3_ARMED", False) and envb("LIVE_TRADING_KC3", True) and envb("LIVE_TRADING", True),
        dry_run=envb("DRY_RUN", False) or envb("SIMULATE", False) or (not envb("LIVE_TRADING", True)),

        quote_asset=envs("KC3_QUOTE_ASSET", envs("QUOTE_ASSET","USDT")),
        symbol_suffix=envs("KC3_SYMBOL_SUFFIX", envs("SYMBOL_SUFFIX","USDT")),
        usd_notional=envf("KC3_USD_NOTIONAL", 25.0),

        zmap_path=envs("KC3_ZMAP_PATH","kc3_zmap.json"),
        lag_z_enter=envf("KC3_LAG_Z_ENTER", 1.6),
        lag_z_exit=envf("KC3_LAG_Z_EXIT", 0.2),
        lag_mode=envs("KC3_LAG_MODE","both").lower(),

        lev_mode=envs("KC3_LEV_MODE","dynamic").lower(),
        lev_min=envf("KC3_LEV_MIN", 5),
        lev_base=envf("KC3_LEV_BASE", 10),
        lev_max=envf("KC3_LEV_MAX", 15),
        lev_z_full=envf("KC3_LEV_Z_FULL", 2.6),

        tp_mode=envs("KC3_TP_MODE","fixed").lower(),
        tp_pct_fixed=envf("KC3_TP_PCT", 0.005),
        tp_check_sec=envf("KC3_TP_CHECK_SEC", 1.0),
        tp_vol_lookback_sec=envf("KC3_TP_VOL_LOOKBACK_SEC", 21600),
        tp_k=envf("KC3_TP_K", 1.8),
        tp_min=envf("KC3_TP_MIN", 0.003),
        tp_max=envf("KC3_TP_MAX", 0.012),

        roll_tp_drop=envf("KC3_ROLL_TP_DROP", 0.25),
        edge_stop_enabled=envb("KC3_EDGE_STOP_ENABLED", True),
        edge_stop_lev_dd=envf("KC3_EDGE_STOP_LEV_DD", 0.05),

        min_hold_sec=envf("KC3_MIN_HOLD_SEC", 60),
        wait_after_close_sec=envf("KC3_WAIT_AFTER_CLOSE_SEC", 2),
    )

def sign(secret: str, query: str) -> str:
    return hmac.new(secret.encode("utf-8"), query.encode("utf-8"), hashlib.sha256).hexdigest()

def req(C: Cfg, method: str, path: str, params: Dict[str, Any], signed: bool=False) -> requests.Response:
    url = C.base_url + path
    headers = {"X-MBX-APIKEY": C.api_key} if C.api_key else {}
    p = dict(params or {})
    if signed:
        p["timestamp"] = int(time.time()*1000)
        p.setdefault("recvWindow", 5000)
        qs = urllib.parse.urlencode(p, doseq=True)
        p["signature"] = sign(C.api_secret, qs)
    r = requests.request(method, url, params=p, headers=headers, timeout=10)
    r.raise_for_status()
    return r

def safe_req(C: Cfg, method: str, path: str, params: Dict[str, Any], signed: bool=False) -> Tuple[Optional[dict], Optional[str]]:
    try:
        r = req(C, method, path, params, signed=signed)
        try:
            return r.json(), None
        except Exception:
            return {"text": r.text}, None
    except requests.HTTPError as e:
        body = ""
        try:
            body = e.response.text
        except Exception:
            pass
        return None, f"HTTPError {method} {path} status={getattr(e.response,'status_code',None)} body={body[:500]}"
    except Exception as e:
        return None, f"{type(e).__name__}: {e}"

def get_positions(C: Cfg) -> List[dict]:
    j, err = safe_req(C, "GET", "/fapi/v2/positionRisk", {}, signed=True)
    if err:
        log(f"FAIL positions {err}")
        return []
    return [x for x in (j or []) if abs(float(x.get("positionAmt") or 0.0)) > 0.0]

def get_all_positions_raw(C: Cfg) -> List[dict]:
    j, err = safe_req(C, "GET", "/fapi/v2/positionRisk", {}, signed=True)
    if err:
        log(f"FAIL positions_raw {err}")
        return []
    return j or []

def get_price(C: Cfg, symbol: str) -> Optional[float]:
    j, err = safe_req(C, "GET", "/fapi/v1/ticker/price", {"symbol": symbol}, signed=False)
    if err:
        log(f"FAIL price {symbol} {err}")
        return None
    try:
        return float(j["price"])
    except Exception:
        return None

def set_leverage(C: Cfg, symbol: str, lev: float):
    lev_i = int(round(float(lev)))
    # clamp
    lev_i = max(int(C.lev_min), min(int(C.lev_max), lev_i))
    if C.dry_run or (not C.armed):
        log(f"SET_LEVERAGE_DRY symbol={symbol} leverage={lev_i}")
        return
    j, err = safe_req(C, "POST", "/fapi/v1/leverage", {"symbol": symbol, "leverage": lev_i}, signed=True)
    if err:
        log(f"SET_LEVERAGE_FAIL symbol={symbol} leverage={lev_i} {err}")
        return
    log(f"SET_LEVERAGE_OK symbol={symbol} leverage={j.get('leverage', lev_i)}")

def order_market(C: Cfg, symbol: str, side: str, qty: float, reduce_only: bool=False):
    qty = float(qty)
    if qty <= 0:
        return
    if C.dry_run or (not C.armed):
        log(f"ORDER_DRY symbol={symbol} side={side} qty={qty:.8f} reduceOnly={reduce_only}")
        return
    params = {
        "symbol": symbol,
        "side": side,
        "type": "MARKET",
        "quantity": f"{qty:.8f}",
        "reduceOnly": "true" if reduce_only else "false",
        "newOrderRespType": "RESULT",
    }
    j, err = safe_req(C, "POST", "/fapi/v1/order", params, signed=True)
    if err:
        log(f"ORDER_FAIL symbol={symbol} side={side} qty={qty:.8f} reduceOnly={reduce_only} {err}")
        return
    log(f"ORDER_OK symbol={symbol} side={side} qty={qty:.8f} reduceOnly={reduce_only}")

def read_zmap(path: str) -> Dict[str, Any]:
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f) or {}
    except Exception:
        return {}

def want_from_z(C: Cfg, z: float, prev_state: int) -> int:
    """
    Hysteresis:
      - If flat: enter when |z| >= z_enter
      - If in position: exit only when |z| <= z_exit
    Direction:
      - z > + => mean reversion expects alt underperformed => LONG alt (target +1)
      - z < - => SHORT alt (target -1)
    """
    az = abs(z)
    if prev_state == 0:
        if az >= C.lag_z_enter:
            return +1 if z > 0 else -1
        return 0
    else:
        # keep until fully reverted
        if az <= C.lag_z_exit:
            return 0
        return prev_state

def lev_from_z(C: Cfg, z: float) -> float:
    if C.lev_mode != "dynamic":
        return float(C.lev_base)
    az = abs(z)
    frac = min(1.0, az / max(1e-9, C.lev_z_full))
    lev = C.lev_base + frac * (C.lev_max - C.lev_base)
    return max(C.lev_min, min(C.lev_max, lev))

def roi_from_pos(pos: dict) -> float:
    # Binance fields vary; try robustly
    upnl = float(pos.get("unRealizedProfit") or 0.0)
    im = pos.get("isolatedMargin")
    if im is None or float(im) == 0.0:
        im = pos.get("positionInitialMargin") or pos.get("initialMargin") or 0.0
    denom = float(im) if float(im) != 0.0 else 0.0
    if denom <= 0:
        # fallback to notional/lev approx
        entry = float(pos.get("entryPrice") or 0.0)
        amt = abs(float(pos.get("positionAmt") or 0.0))
        notional = entry * amt
        lev = float(pos.get("leverage") or 1.0)
        denom = notional / max(1.0, lev)
    if denom <= 0:
        return 0.0
    return upnl / denom

class TPVol:
    def __init__(self, maxlen: int):
        self.px = deque(maxlen=maxlen)
        self.ts = deque(maxlen=maxlen)

    def add(self, t: float, price: float):
        self.ts.append(t)
        self.px.append(price)

    def vol(self, lookback_sec: float) -> float:
        if len(self.px) < 5:
            return 0.0
        now = self.ts[-1]
        # collect log returns within lookback
        rets = []
        for i in range(1, len(self.px)):
            if now - self.ts[i-1] > lookback_sec:
                continue
            p0 = self.px[i-1]; p1 = self.px[i]
            if p0 > 0 and p1 > 0:
                rets.append(math.log(p1/p0))
        if len(rets) < 5:
            return 0.0
        m = sum(rets)/len(rets)
        v = sum((r-m)*(r-m) for r in rets)/max(1, (len(rets)-1))
        return math.sqrt(max(0.0, v))

def main():
    C = load_cfg()
    if not C.api_key or not C.api_secret:
        log("FATAL missing BINANCE API keys (BINANCE_FAPI_KEY/SECRET or BINANCE_API_KEY/SECRET)")
        raise SystemExit(2)

    log(f"BOOT armed={int(C.armed)} dry_run={int(C.dry_run)} poll={C.poll_sec}s tp_mode={C.tp_mode} lev_mode={C.lev_mode} z_enter={C.lag_z_enter} z_exit={C.lag_z_exit}")
    state_path = "kc3_exec_state.json"
    state = {"hyst": {}, "peak_roi": {}, "opened_ts": {}, "last_tp_check": 0.0}
    try:
        with open(state_path, "r", encoding="utf-8") as f:
            state.update(json.load(f) or {})
    except Exception:
        pass

    vol_buf: Dict[str, TPVol] = {}

    while True:
        t0 = time.time()
        try:
            zmap = read_zmap(C.zmap_path)
            # zmap format expected: { "BNBUSDT": {"z": -2.41, ...}, ... } OR { "BNBUSDT": -2.41 }
            # build simple z lookup
            z_of: Dict[str, float] = {}
            for k,v in (zmap or {}).items():
                try:
                    if isinstance(v, dict) and "z" in v:
                        z_of[k] = float(v["z"])
                    else:
                        z_of[k] = float(v)
                except Exception:
                    continue

            # pull all positions (including zero) so we can close + track
            pos_all = get_all_positions_raw(C)
            pos_by_sym = {p.get("symbol"): p for p in pos_all if p.get("symbol")}

            # ---- EXIT ENGINE (rolling TP + edge stop + dynamic TP) ----
            open_positions = [p for p in pos_all if abs(float(p.get("positionAmt") or 0.0)) > 0.0]
            for p in open_positions:
                sym = p["symbol"]
                amt = float(p.get("positionAmt") or 0.0)
                side = "SELL" if amt > 0 else "BUY"   # reduceOnly close side
                roi = roi_from_pos(p)

                # track peak ROI
                peak = float(state["peak_roi"].get(sym, -1e9))
                if roi > peak:
                    state["peak_roi"][sym] = roi
                    peak = roi

                # edge stop: close if ROI drawdown exceeds threshold
                if C.edge_stop_enabled and roi <= -abs(C.edge_stop_lev_dd):
                    log(f"EDGE_STOP symbol={sym} roi={roi:.5f} thresh={-abs(C.edge_stop_lev_dd):.5f}")
                    order_market(C, sym, side, abs(amt), reduce_only=True)
                    state["opened_ts"].pop(sym, None)
                    state["peak_roi"].pop(sym, None)
                    time.sleep(C.wait_after_close_sec)
                    continue

                # rolling TP: if fell from peak by fraction
                if peak > 0 and roi < peak * (1.0 - max(0.0, C.roll_tp_drop)):
                    log(f"ROLL_TP symbol={sym} roi={roi:.5f} peak={peak:.5f} drop={C.roll_tp_drop:.3f}")
                    order_market(C, sym, side, abs(amt), reduce_only=True)
                    state["opened_ts"].pop(sym, None)
                    state["peak_roi"].pop(sym, None)
                    time.sleep(C.wait_after_close_sec)
                    continue

                # dynamic TP (close when ROI >= tp_target)
                now = time.time()
                if now - float(state.get("last_tp_check", 0.0)) >= C.tp_check_sec:
                    tp_target = C.tp_pct_fixed
                    # maintain volatility buffer
                    px = get_price(C, sym)
                    if px is not None:
                        vb = vol_buf.get(sym)
                        if vb is None:
                            vb = TPVol(maxlen=4000)
                            vol_buf[sym] = vb
                        vb.add(now, px)
                        if C.tp_mode == "vol":
                            vol = vb.vol(C.tp_vol_lookback_sec)
                            # convert vol of log-returns to pct-ish target
                            tp_target = max(C.tp_min, min(C.tp_max, C.tp_k * vol))
                    if tp_target > 0 and roi >= tp_target:
                        log(f"TP_HIT symbol={sym} roi={roi:.5f} tp_target={tp_target:.5f} mode={C.tp_mode}")
                        order_market(C, sym, side, abs(amt), reduce_only=True)
                        state["opened_ts"].pop(sym, None)
                        state["peak_roi"].pop(sym, None)
                        time.sleep(C.wait_after_close_sec)
                        continue

            state["last_tp_check"] = time.time()

            # ---- ENTRY / POSITION DIRECTION ENGINE (zscore hysteresis) ----
            # For each symbol in zmap: decide want state (+1 long / -1 short / 0 flat)
            for sym, z in z_of.items():
                prev = int(state["hyst"].get(sym, 0))
                want = want_from_z(C, z, prev)
                state["hyst"][sym] = want

                # optional directional filter
                if C.lag_mode == "long" and want < 0:
                    want = 0
                if C.lag_mode == "short" and want > 0:
                    want = 0

                pos = pos_by_sym.get(sym)
                cur_amt = float(pos.get("positionAmt") or 0.0) if pos else 0.0
                cur_side = 0
                if cur_amt > 0: cur_side = +1
                elif cur_amt < 0: cur_side = -1

                # dynamic leverage from z
                lev = lev_from_z(C, z)
                # only set leverage when we intend to be in a position (or already are)
                if want != 0 or cur_side != 0:
                    set_leverage(C, sym, lev)

                # If we want flat but have a position -> close
                if want == 0 and cur_side != 0:
                    close_side = "SELL" if cur_amt > 0 else "BUY"
                    log(f"CLOSE_Z symbol={sym} z={z:.4f} cur_side={cur_side} -> want=0")
                    order_market(C, sym, close_side, abs(cur_amt), reduce_only=True)
                    state["opened_ts"].pop(sym, None)
                    state["peak_roi"].pop(sym, None)
                    time.sleep(C.wait_after_close_sec)
                    continue

                # If want direction differs from current -> flip (close then open)
                if want != 0 and want != cur_side:
                    if cur_side != 0:
                        close_side = "SELL" if cur_amt > 0 else "BUY"
                        log(f"FLIP_CLOSE symbol={sym} z={z:.4f} cur_side={cur_side} -> want={want}")
                        order_market(C, sym, close_side, abs(cur_amt), reduce_only=True)
                        state["peak_roi"].pop(sym, None)
                        time.sleep(C.wait_after_close_sec)

                    # open new position
                    px = get_price(C, sym)
                    if px is None or px <= 0:
                        continue
                    # qty based on usd_notional * leverage / price
                    notional = max(0.0, C.usd_notional) * float(lev)
                    qty = notional / px
                    open_side = "BUY" if want > 0 else "SELL"
                    log(f"OPEN symbol={sym} want={want} z={z:.4f} lev={lev:.2f} px={px:.6f} qty={qty:.8f}")
                    order_market(C, sym, open_side, qty, reduce_only=False)
                    state["opened_ts"][sym] = time.time()
                    state["peak_roi"][sym] = -1e9
                    continue

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
