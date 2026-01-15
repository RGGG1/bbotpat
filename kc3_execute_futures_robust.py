#!/usr/bin/env python3
import os
import time
import json
import math
import traceback
from pathlib import Path
from datetime import datetime, timezone

import kc3_edge_stop
import kc3_execute_futures as base

STATUS  = Path("/var/www/bbotpat_live/kc3_futures_status.json")
DESIRED = Path("/root/bbotpat_live/kc3_desired_position.json")
ZMAP    = Path("/root/bbotpat_live/kc3_zmap.json")
STATE   = Path("/root/bbotpat_live/data/kc3_exec_state.json")
STATE.parent.mkdir(parents=True, exist_ok=True)

RECONCILE_SEC = float(os.getenv("KC3_RECONCILE_SEC", "15"))
HEARTBEAT_SEC = float(os.getenv("KC3_HEARTBEAT_SEC", "60"))

TP_PCT = float(os.getenv("KC3_TP_PCT", "0.0"))
SL_PCT = float(os.getenv("KC3_SL_PCT", "0.0"))

ROTATE_MIN_ROI      = float(os.getenv("KC3_ROTATE_MIN_ROI", "0.0"))   # e.g. 0.002 => +0.2%
ALLOW_OPEN_ON_HOLD  = int(os.getenv("KC3_ALLOW_OPEN_ON_HOLD", "1"))    # 0 = do not open from HOLD when flat
MAX_CAND_TRIES      = int(os.getenv("KC3_MAX_CAND_TRIES", "7"))        # for FLAT close scanning

# --- KC3_FILELOG_HELPER ---
def _kc3_filelog(msg: str, path: str = "kc3_exec.log"):
    try:
        with open(path, "a", encoding="utf-8") as f:
            f.write(str(msg).rstrip("\n") + "\n")
    except Exception:
        # Never let logging kill the bot
        pass

def utc() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")

def _safe_read_json(p: Path, default):
    try:
        if not p.exists():
            return default
        txt = p.read_text(encoding="utf-8", errors="replace") or ""
        return json.loads(txt) if txt.strip() else default
    except Exception:
        return default

def write_status(o):
    """
    Atomically write STATUS.

    IMPORTANT: heartbeat must NOT clobber prior meaningful status fields.
    If payload.note == 'heartbeat', we merge into existing JSON and only
    overwrite keys present in payload (ts/alive/note).
    """
    payload = o if isinstance(o, dict) else {"note": str(o)}
    if not isinstance(payload, dict):
        payload = {"note": str(payload)}

    # Merge heartbeat into previous status
    if payload.get("note") == "heartbeat":
        prev = _safe_read_json(STATUS, {})
        if isinstance(prev, dict):
            merged = dict(prev)
            merged.update(payload)
            payload = merged

    tmp = STATUS.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    tmp.replace(STATUS)

def load_state():
    return _safe_read_json(STATE, {})

def save_state(s):
    STATE.write_text(json.dumps(s, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

def edge_stop_cfg():
    enabled = (os.getenv("KC3_EDGE_STOP_ENABLED", "0").strip().lower() in ("1", "true", "yes", "on"))
    def f(k, d):
        try:
            return float(os.getenv(k, str(d)))
        except Exception:
            return float(d)
    def i(k, d):
        try:
            return int(os.getenv(k, str(d)))
        except Exception:
            return int(d)

    return kc3_edge_stop.EdgeStopConfig(
        enabled=enabled,
        lev_dd=f("KC3_EDGE_STOP_LEV_DD", 0.08),
        z_revert=f("KC3_EDGE_STOP_Z_REVERT", 0.55),
        z_vel_cycles=i("KC3_EDGE_STOP_Z_VEL_CYCLES", 3),
        no_bounce=f("KC3_EDGE_STOP_NO_BOUNCE", 0.02),
        hard_max_lev_dd=f("KC3_HARD_MAX_LEV_DD", 0.15),
        z_hist_max=8,
    )

def read_desired():
    return _safe_read_json(DESIRED, None)

def read_zmap():
    return _safe_read_json(ZMAP, {})

def _clamp(x, lo, hi):
    return max(lo, min(hi, x))

def _tok(symbol: str) -> str:
    if not symbol or not isinstance(symbol, str):
        return ""
    return symbol.replace("USDT", "").upper().strip()

def _read_lag_history():
    try:
        fp = Path(__file__).resolve().parent / "data" / "kc3_lag_state.json"
        d = _safe_read_json(fp, {})
        h = d.get("history") or []
        return h if isinstance(h, list) else []
    except Exception:
        return []

def dynamic_tp_threshold(symbol: str, default_tp: float):
    mode = (os.getenv("KC3_TP_MODE", "") or "").strip().lower()
    if mode != "vol":
        return default_tp, "fixed", None

    tok = _tok(symbol)
    if not tok:
        return default_tp, "fixed", None

    lookback_sec = float(os.getenv("KC3_TP_VOL_LOOKBACK_SEC", "21600") or "21600")
    k      = float(os.getenv("KC3_TP_K", "1.8") or "1.8")
    tp_min = float(os.getenv("KC3_TP_MIN", "0.003") or "0.003")
    tp_max = float(os.getenv("KC3_TP_MAX", "0.012") or "0.012")

    hist = _read_lag_history()
    if len(hist) < 10:
        return default_tp, "fixed", None

    pts = max(20, int(lookback_sec / 15.0))
    window = hist[-pts:]

    vals = []
    for entry in window:
        if not isinstance(entry, dict):
            continue
        rel = entry.get("rel")
        if not isinstance(rel, dict):
            continue
        v = rel.get(tok)
        if v is None:
            continue
        try:
            vals.append(float(v))
        except Exception:
            pass

    if len(vals) < 10:
        return default_tp, "fixed", None

    mu = sum(vals) / len(vals)
    var = sum((x - mu) ** 2 for x in vals) / len(vals)
    vol = math.sqrt(var)

    tp = _clamp(k * vol, tp_min, tp_max)
    return tp, "vol", vol

def current_roi(symbol):
    pos = base.get_position(symbol)
    if not pos or float(pos.get("amt", 0) or 0) == 0.0:
        return None

    entry = float(pos["entry"])
    mark  = float(base.get_mark(symbol))
    amt   = float(pos["amt"])
    if entry <= 0:
        return None

    side = "LONG" if amt > 0 else "SHORT"
    if side == "LONG":
        return (mark - entry) / entry
    else:
        return (entry - mark) / entry

def symbols_to_scan(desired):
    syms = []
    if isinstance(desired, dict):
        s = desired.get("symbol")
        if isinstance(s, str) and s.endswith("USDT"):
            syms.append(s)
        for x in (desired.get("candidates") or []):
            if isinstance(x, str) and x.endswith("USDT"):
                syms.append(x)
        for tok in (desired.get("alt_list") or []):
            if isinstance(tok, str) and tok.strip():
                syms.append(tok.strip().upper() + "USDT")

    seen = set()
    out = []
    for s in syms:
        if s not in seen:
            seen.add(s)
            out.append(s)
    return out

def close_other_positions(keep_symbol, desired):
    closed = []
    for sym in symbols_to_scan(desired):
        if sym == keep_symbol:
            continue
        try:
            pos = base.get_position(sym)
            if pos and float(pos.get("amt", 0) or 0) != 0.0:
                base.close_position(sym)
                closed.append(sym)
                print(f"[{utc()}] KC3 FORCE CLOSE {sym} (single-position rule)", flush=True)
                time.sleep(1)
        except Exception:
            pass
    return closed

def _kc3_is_margin_insufficient_msg(msg: str) -> bool:
    if not msg:
        return False
    return ('"code":-2019' in msg) or ("'code': -2019" in msg) or ("Margin is insufficient" in msg)

def main():
    last_beat = 0.0
    last_reconcile = 0.0
    last_tp_check = 0.0

    state = load_state()
    desired = None

    print(f"[{utc()}] ROBUST wrapper started (TP+SL)", flush=True)

    while True:
        try:
            now = time.time()

            # Heartbeat
            if now - last_beat >= HEARTBEAT_SEC:
                write_status({
                    "ts": utc(),
                    "alive": True,
                    "note": "heartbeat",
                    "tracked": {"symbol": state.get("symbol"), "side": state.get("side"), "last_roi": state.get("last_roi")}
                })
                last_beat = now

            # TP/SL + edge-stop check (1s)
            if now - last_tp_check >= 1.0:
                sym = state.get("symbol")
                side = state.get("side")

                if sym and side:
                    try:
                        roi = current_roi(sym)
                    except Exception as e:
                        print(f"[{utc()}] WARN current_roi failed {sym} err={e}", flush=True)
                        roi = None

                    if roi is not None:
                        state["last_roi"] = roi

                    # Edge-stop (optional)
                    try:
                        cfg = edge_stop_cfg()
                        if cfg.enabled and sym and side and isinstance(desired, dict):
                            zmap = read_zmap()
                            z_now = None
                            try:
                                z_now = zmap.get(sym)
                            except Exception:
                                z_now = None

                            kc3_edge_stop.set_entry_z_if_missing(
                                state,
                                state.get("edge_stop", {}).get("entry_z") or desired.get("z_score")
                            )

                            _p = base.get_position(sym) or {}
                            lev = float(_p.get("leverage") or 0.0)
                            lev_roi = (float(roi) * lev) if (roi is not None and lev) else None

                            kc3_edge_stop.update_edge_state(state, z_now=z_now, lev_roi=lev_roi, symbol=sym, side=side)
                            do_stop, reason, details = kc3_edge_stop.should_edge_stop(state, cfg, z_now=z_now, lev_roi=lev_roi)

                            es = state.setdefault("edge_stop", {})
                            last = float(es.get("last_log_ts") or 0)
                            if time.time() - last >= 60:
                                es["last_log_ts"] = time.time()
                                print(f"[{utc()}] EDGE_STOP chk sym={sym} lev_roi={lev_roi} z_now={z_now} -> {reason}", flush=True)
                                save_state(state)

                            if do_stop:
                                write_status({"ts": utc(), "alive": True, "note": reason, "details": details, "desired": desired})
                                if isinstance(desired, dict) and desired.get("signal_id"):
                                    state["cooldown_signal_id"] = desired.get("signal_id")
                                base.close_position(sym, reason=reason)
                    except Exception:
                        pass

                last_tp_check = now

            # Reconcile desired (every RECONCILE_SEC)
            if now - last_reconcile >= RECONCILE_SEC:
                desired = read_desired()

                # If desired missing, just wait
                if not isinstance(desired, dict):
                    last_reconcile = now
                    time.sleep(1)
                    continue

                d_side = str(desired.get("side", "")).upper()
                d_sym  = desired.get("symbol")
                d_reason = str(desired.get("reason", ""))
                d_signal_id = desired.get("signal_id")

                # FLAT handling
                if d_side == "FLAT":
                    attempts = []
                    closed_any = False
                    syms = symbols_to_scan(desired)[:MAX_CAND_TRIES]
                    for sym in syms:
                        try:
                            pos = base.get_position(sym)
                            if pos and float(pos.get("amt", 0) or 0) != 0.0:
                                ok = base.close_position(sym)
                                attempts.append({"symbol": sym, "ok": bool(ok), "action": "close"})
                                closed_any = closed_any or bool(ok)
                        except Exception as e:
                            msg = str(e)
                            if _kc3_is_margin_insufficient_msg(msg):
                                cd = float(os.getenv("KC3_MARGIN_COOLDOWN_SEC", "120") or "120")
                                state["cooldown"] = "margin"
                                state["cooldown_signal_id"] = state.get("open_signal_id")
                                state["cooldown_until"] = time.time() + cd
                                save_state(state)
                                print(f"[{utc()}] KC3 MARGIN_INSUFFICIENT (-2019) -> cooldown {int(cd)}s", flush=True)
                                time.sleep(1)
                                continue
                            attempts.append({"symbol": sym, "action": "exception", "err": repr(e)})

                    state.update({"symbol": None, "side": None})
                    save_state(state)
                    write_status({
                        "ts": utc(),
                        "alive": True,
                        "note": "flat",
                        "desired": desired,
                        "result": {"closed_any": closed_any, "attempts": attempts},
                    })

                    last_reconcile = now
                    time.sleep(RECONCILE_SEC)
                    continue

                # Validate symbol
                if not isinstance(d_sym, str) or not d_sym.endswith("USDT"):
                    last_reconcile = now
                    time.sleep(1)
                    continue

                # If flat + HOLD and open-on-hold disabled
                if (not state.get("symbol")) and d_reason == "hold" and ALLOW_OPEN_ON_HOLD == 0:
                    write_status({"ts": utc(), "alive": True, "note": "flat_wait_fresh_signal", "desired": desired})
                    last_reconcile = now
                    time.sleep(1)
                    continue

                # Fresh-signal gate (don’t re-enter same signal_id after stop)
                if (not state.get("symbol")) and d_side in ("LONG", "SHORT") and d_signal_id:
                    if state.get("cooldown_signal_id") == d_signal_id:
                        write_status({"ts": utc(), "alive": True, "note": "flat_wait_fresh_signal", "desired": desired})
                        last_reconcile = now
                        time.sleep(1)
                        continue
                    state["cooldown_signal_id"] = None
                    state["cooldown"] = None

                cur_sym = state.get("symbol")
                cur_side = state.get("side")

                # Rotation ROI gate
                if cur_sym and cur_side and (cur_sym != d_sym or cur_side != d_side):
                    roi = current_roi(cur_sym)
                    if roi is None:
                        roi = 0.0
                    if roi < ROTATE_MIN_ROI:
                        write_status({
                            "ts": utc(),
                            "alive": True,
                            "note": "hold_roi_gate",
                            "roi": roi,
                            "rotate_min_roi": ROTATE_MIN_ROI,
                            "have": {"symbol": cur_sym, "side": cur_side},
                            "desired": desired,
                        })
                        last_reconcile = now
                        time.sleep(1)
                        continue

                # Enforce single-position rule over tracked universe
                close_other_positions(d_sym, desired)

                # Reconcile desired symbol/side
                pos = base.get_position(d_sym)
                have = "FLAT"
                if pos and float(pos.get("amt", 0) or 0) > 0:
                    have = "LONG"
                if pos and float(pos.get("amt", 0) or 0) < 0:
                    have = "SHORT"

                if have == d_side:
                    state.update({"symbol": d_sym, "side": d_side})
                else:
                    if have != "FLAT":
                        base.close_position(d_sym)
                        time.sleep(2)

                    base.open_position(
                        symbol=d_sym,
                        desired_side=d_side,
                        z_score=desired.get("z_score"),
                        hmi=desired.get("hmi"),
                        hmi_delta=desired.get("hmi_delta"),
                        src="robust",
                    )
                    state.update({
                        "symbol": d_sym,
                        "side": d_side,
                        "open_signal_id": d_signal_id,
                    })

                save_state(state)
                write_status({"ts": utc(), "alive": True, "note": "reconciled", "desired": desired})
                last_reconcile = now

            time.sleep(0.5)

        except Exception as e:
            tb = traceback.format_exc()
            traceback.print_exc()
            write_status({
                "ts": utc(),
                "alive": True,
                "note": "error",
                "err": repr(e),
                "tb": tb[-2000:],
            })
            _kc3_filelog(f"[{utc()}] ERROR {repr(e)}\n{tb}")
            time.sleep(5)

if __name__ == "__main__":
    main()
