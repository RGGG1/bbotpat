#!/usr/bin/env python3
import os, time, json, math, traceback
from pathlib import Path
from datetime import datetime, timezone


def json.load(open("data/kc3_token_universe.json")):
    alt = os.getenv("KC3_ALT_LIST", "").strip()
    if alt:
        alt = alt.replace(",", " ")
        alt = " ".join(alt.split())
        toks = [t.upper() for t in alt.split() if t.strip()]
    else:
        toks = []
    # de-dupe preserve order
    seen=set()
    out=[]
    for t in toks:
        if t not in seen:
            seen.add(t)
            out.append(t)
    return out


PRICES_IN   = Path("/var/www/bbotpat_live/prices_latest.json")
DESIRED_OUT = Path("/root/bbotpat_live/kc3_desired_position.json")
ZMAP_OUT   = Path("/root/bbotpat_live/kc3_zmap.json")
STATE_PATH  = Path("/root/bbotpat_live/data/kc3_lag_state.json")
STATE_PATH.parent.mkdir(parents=True, exist_ok=True)

ALT_LIST = json.load(open("data/kc3_token_universe.json"))
USD_NOTIONAL = float(os.getenv("KC3_USD_NOTIONAL", "25"))

LOOKBACK_SEC = float(os.getenv("KC3_LAG_LOOKBACK_SEC", "900"))   # 15m default
LOOP_SEC     = float(os.getenv("KC3_LAG_LOOP_SEC", "15"))        # agent loop
Z_ENTER      = float(os.getenv("KC3_LAG_Z_ENTER", "1.2"))        # enter threshold
Z_EXIT       = float(os.getenv("KC3_LAG_Z_EXIT", "0.3"))         # exit threshold (hysteresis)
SWITCH_DELTA = float(os.getenv("KC3_LAG_SWITCH_DELTA", "0.3"))   # rotate only if better by this
MAX_HIST_LEN = int(os.getenv("KC3_LAG_MAX_HIST_LEN", "2000"))    # safety cap

def utc():
    return datetime.now(timezone.utc).isoformat().replace("+00:00","Z")

def safe_read_json(p: Path):
    try:
        if not p.exists():
            return None
        return json.loads(p.read_text())
    except Exception:
        return None

def safe_write_json(p: Path, obj):
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(obj, indent=2) + "\n")
    tmp.replace(p)

# ---- Price helper for prices_latest.json (expects {'timestamp':..., 'rows':[{'token','price'},...]}) ----
def get_px(prices_doc, token: str):
    try:
        token = str(token).upper()
    except Exception:
        return None
    if not isinstance(prices_doc, dict):
        return None
    rows = prices_doc.get("rows")
    if isinstance(rows, list):
        for r in rows:
            if isinstance(r, dict) and str(r.get("token","")).upper() == token:
                try:
                    v = float(r.get("price"))
                    return v if v > 0 else None
                except Exception:
                    return None
    # fallback
    try:
        v = float(prices_doc.get(token))
        return v if v > 0 else None
    except Exception:
        return None

def load_state():
    st = safe_read_json(STATE_PATH)
    return st if isinstance(st, dict) else {}

def save_state(st: dict):
    safe_write_json(STATE_PATH, st)

def prune_history(hist, now_ts: float):
    # keep only within LOOKBACK_SEC
    if not isinstance(hist, list):
        return []
    cutoff = now_ts - LOOKBACK_SEC
    hist2 = [x for x in hist if isinstance(x, dict) and float(x.get("t", 0) or 0) >= cutoff]
    return hist2[-MAX_HIST_LEN:]

def build_snapshot(prices_doc):
    """
    Returns snapshot dict: {"t": epoch, "rel": {tok: log((ALT/BTC) / (ALT0/BTC0))?}}
    Here we store log(ALT/BTC) itself; returns are differences of logs across window.
    """
    btc = get_px(prices_doc, "BTC")
    if not btc:
        return None
    rel = {}
    for tok in ALT_LIST:
        px = get_px(prices_doc, tok)
        if px and px > 0:
            rel[tok] = math.log(px / btc)
    if len(rel) < 2:
        return None
    return {"t": time.time(), "rel": rel}

def compute_returns(hist):
    # returns = latest_rel - oldest_rel for tokens present in both
    if not isinstance(hist, list) or len(hist) < 2:
        return None
    oldest = hist[0].get("rel") if isinstance(hist[0], dict) else None
    latest = hist[-1].get("rel") if isinstance(hist[-1], dict) else None
    if not isinstance(oldest, dict) or not isinstance(latest, dict):
        return None
    rets = {}
    for tok, v in latest.items():
        if tok in oldest:
            try:
                rets[tok] = float(v) - float(oldest[tok])
            except Exception:
                pass
    return rets if len(rets) >= 2 else None

def mean_std(vals):
    vs = [float(x) for x in vals]
    m = sum(vs)/len(vs)
    var = sum((x-m)**2 for x in vs)/len(vs)
    return m, math.sqrt(var)

def make_signal_id(side, symbol, z_score, prices_ts):
    # "fresh" identifier changes whenever we create a new enter/rotate decision
    return f"{utc()}|{side}|{symbol}|z={round(float(z_score),4)}|pts={prices_ts}"

def main():
    print(f"[{utc()}] KC3 LAG agent started | alts={ALT_LIST} lookback_sec={LOOKBACK_SEC} z_enter={Z_ENTER} loop={LOOP_SEC}s", flush=True)

    while True:
        try:
            st = load_state()
            prices_doc = safe_read_json(PRICES_IN)
            prices_ts = (prices_doc.get("timestamp") if isinstance(prices_doc, dict) else None) or utc()

            snap = build_snapshot(prices_doc)
            if snap is None:
                # If can't compute dispersion, ask executor to go/stay flat
                desired = {
                    "side": "FLAT",
                    "symbol": "",
                    "notional_usd": float(USD_NOTIONAL),
                    "timestamp": utc(),
                    "src": "lag_selector",
                    "reason": "no_prices",
                    "alt_list": ALT_LIST,
                    "prices_ts": prices_ts,
                    "candidates": [f"{tok}USDT" for tok in ALT_LIST],
                }
                safe_write_json(DESIRED_OUT, desired)
                safe_write_json(ZMAP_OUT, {})
                print(f"[{utc()}] no prices; FLAT", flush=True)
                time.sleep(LOOP_SEC)
                continue

            hist = st.get("history") or []
            hist.append(snap)
            hist = prune_history(hist, snap["t"])
            st["history"] = hist

            rets = compute_returns(hist)
            if rets is None:
                save_state(st)
                print(f"[{utc()}] warming up history ({len(hist)} pts)", flush=True)
                time.sleep(LOOP_SEC)
                continue

            m, s = mean_std(rets.values())
            if s <= 0:
                save_state(st)
                desired = {
                    "side": "FLAT",
                    "symbol": "",
                    "notional_usd": float(USD_NOTIONAL),
                    "timestamp": utc(),
                    "src": "lag_selector",
                    "reason": "zero_dispersion",
                    "alt_list": ALT_LIST,
                    "prices_ts": prices_ts,
                    "candidates": [f"{tok}USDT" for tok in ALT_LIST],
                }
                safe_write_json(DESIRED_OUT, desired)
                safe_write_json(ZMAP_OUT, {})
                print(f"[{utc()}] zero dispersion; FLAT", flush=True)
                time.sleep(LOOP_SEC)
                continue

            z = {tok: (ret - m)/s for tok, ret in rets.items()}

            # --- Emit full z-map each cycle (all symbols, missing -> null) ---
            try:
                zmap_out = {}
                for tok in ALT_LIST:
                    sym = f"{str(tok).upper()}USDT"
                    v = z.get(str(tok).upper())
                    zmap_out[sym] = (float(v) if v is not None else None)
                safe_write_json(ZMAP_OUT, zmap_out)
            except Exception:
                pass

            # --- emit z-map for executor (symbol->z) ---
            zmap = {f"{tok}USDT": float(val) for tok, val in z.items()}
            safe_write_json(ZMAP_OUT, zmap)


            # sort tokens by abs(z) descending (candidate list)
            ranked = sorted(z.items(), key=lambda kv: abs(kv[1]), reverse=True)
            best_tok, best_z = ranked[0]
            best_side = "LONG" if best_z < 0 else "SHORT"
            best_sym = f"{best_tok}USDT"

            # current position as tracked by agent state
            cur_sym = st.get("cur_symbol")
            cur_side = st.get("cur_side")
            cur_tok = None
            cur_z = None
            if cur_sym and isinstance(cur_sym, str) and cur_sym.endswith("USDT"):
                cur_tok = cur_sym[:-4]
                if cur_tok in z:
                    cur_z = z[cur_tok]

            # Decision engine (A): hysteresis + rotation delta
            desired_side = "FLAT"
            desired_sym  = ""
            reason = "flat"
            z_score = 0.0
            signal_id = st.get("last_signal_id")

            if cur_sym and cur_side and cur_tok in z:
                # We have an active trade tracked by agent
                if abs(cur_z) <= Z_EXIT:
                    desired_side = "FLAT"
                    desired_sym  = ""
                    reason = "exit"
                    z_score = float(cur_z)
                    # when we exit, clear tracked position
                    st["cur_symbol"] = None
                    st["cur_side"] = None
                    st["pos_active"] = False
                else:
                    # consider rotation only if new is clearly better
                    if (best_sym != cur_sym) and (abs(best_z) >= abs(cur_z) + SWITCH_DELTA) and (abs(best_z) >= Z_ENTER):
                        desired_side = best_side
                        desired_sym  = best_sym
                        reason = "rotate"
                        z_score = float(best_z)
                        signal_id = make_signal_id(desired_side, desired_sym, z_score, prices_ts)
                        st["cur_symbol"] = desired_sym
                        st["cur_side"] = desired_side
                        st["pos_active"] = True
                        st["last_signal_id"] = signal_id
                    else:
                        desired_side = cur_side
                        desired_sym  = cur_sym
                        reason = "hold"
                        z_score = float(cur_z)
                        # signal_id remains the last_signal_id (not fresh)
            else:
                # flat
                st["pos_active"] = False
                if abs(best_z) >= Z_ENTER:
                    desired_side = best_side
                    desired_sym  = best_sym
                    reason = "enter"
                    z_score = float(best_z)
                    signal_id = make_signal_id(desired_side, desired_sym, z_score, prices_ts)
                    st["cur_symbol"] = desired_sym
                    st["cur_side"] = desired_side
                    st["pos_active"] = True
                    st["last_signal_id"] = signal_id
                else:
                    desired_side = "FLAT"
                    desired_sym  = ""
                    reason = "flat"
                    z_score = float(best_z)

            save_state(st)

            desired = {
                "side": desired_side,
                "symbol": desired_sym,
                "notional_usd": float(USD_NOTIONAL),
                "timestamp": utc(),
                "src": "lag_selector",
                "reason": reason,
                "alt_list": ALT_LIST,
                "prices_ts": prices_ts,
                "z_score": z_score,
                "signal_id": signal_id,
                "candidates": [f"{tok}USDT" for tok, _ in ranked],
            }
            safe_write_json(DESIRED_OUT, desired)

            if desired_side == "FLAT":
                print(f"[{utc()}] FLAT reason={reason} best={best_tok} z={best_z:.3f}", flush=True)
            else:
                print(f"[{utc()}] {reason.upper()} {desired_side} {desired_sym} z={z_score:.3f} best={best_tok} zbest={best_z:.3f}", flush=True)

            time.sleep(LOOP_SEC)

        except Exception as e:
            print(f"[{utc()}] ERROR {type(e).__name__}: {e}", flush=True)
            traceback.print_exc()
            time.sleep(5)

if __name__ == "__main__":
    main()
