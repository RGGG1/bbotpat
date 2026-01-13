#!/usr/bin/env python3
import os, re, json, time
from datetime import datetime, timezone
from pathlib import Path

AGENT_LOG = Path(os.getenv("KC3_AGENT_LOG", "/root/bbotpat_live/kc3_agent.log"))
EXEC_LOG  = Path(os.getenv("KC3_EXEC_LOG",  "/root/bbotpat_live/kc3_exec.log"))

OUT_DIR   = Path(os.getenv("KC3_AUDIT_DIR", "/root/bbotpat_live/data"))
OUT_DIR.mkdir(parents=True, exist_ok=True)

OUT_JSONL = OUT_DIR / "kc3_trade_audit.jsonl"
OUT_STATE = OUT_DIR / "kc3_trade_audit_state.json"

SLEEP_SEC = float(os.getenv("KC3_AUDIT_SLEEP_SEC", "0.25") or "0.25")

# --- Regexes (keep simple & tolerant) ---
RE_TS = re.compile(r'^\[(?P<ts>[^]]+)\]\s+(?P<msg>.*)$')

# Agent lines examples:
# [..] ENTER LONG TONUSDT z=-1.621 best=TON zbest=-1.621
# [..] HOLD LONG TONUSDT z=-1.50 best=TON zbest=-1.50
# [..] FLAT reason=flat best=TON z=-1.45
RE_AGENT = re.compile(
    r'^(?P<action>ENTER|HOLD|ROTATE)\s+(?P<side>LONG|SHORT)\s+(?P<symbol>[A-Z0-9]+)\s+z=(?P<z>-?[0-9.]+).*(?:best=(?P<best>[A-Z0-9]+))?.*(?:zbest=(?P<zbest>-?[0-9.]+))?'
)
RE_AGENT_FLAT = re.compile(
    r'^FLAT\s+reason=(?P<reason>[a-zA-Z0-9_]+)\s+best=(?P<best>[A-Z0-9]+)\s+z=(?P<z>-?[0-9.]+)'
)

# Executor lines examples:
# [..] KC3 OPEN SHORT SUIUSDT (...) margin~12.1959 ... notional~109.763 qty~59.6 mark~1.8407 ...
# [..] KC3 CLOSE LONG TONUSDT qty=66.0 mark~1.76036 LIVE=True
RE_OPEN = re.compile(
    r'^KC3 OPEN\s+(?P<side>LONG|SHORT)\s+(?P<symbol>[A-Z0-9]+)\s+.*?margin~(?P<margin>[0-9.]+).*?notional~(?P<notional>[0-9.]+).*?qty~(?P<qty>[0-9.]+).*?mark~(?P<mark>[0-9.]+).*?(?:src=(?P<src>[a-zA-Z0-9_]+))?'
)
RE_CLOSE = re.compile(
    r'^KC3 CLOSE\s+(?P<side>LONG|SHORT)\s+(?P<symbol>[A-Z0-9]+)\s+qty=(?P<qty>[0-9.]+).*?mark~(?P<mark>[0-9.]+)'
)

# TP line example:
# [..] TP hit BNBUSDT roi=0.0051 tp_thr=0.0049 mode=vol vol=0.0027...
RE_TP = re.compile(
    r'^TP hit\s+(?P<symbol>[A-Z0-9]+)\s+roi=(?P<roi>-?[0-9.]+)\s+tp_thr=(?P<tp_thr>[0-9.]+)\s+mode=(?P<mode>[a-zA-Z0-9_]+)(?:\s+vol=(?P<vol>-?[0-9.]+))?'
)

# SL line example (if you add later):
RE_SL = re.compile(
    r'^(?:SL hit|STOP hit)\s+(?P<symbol>[A-Z0-9]+)\s+roi=(?P<roi>-?[0-9.]+).*'
)

# Leverage logs (best-effort)
RE_LEV_SET = re.compile(r'LEVERAGE_SET.*?(?P<symbol>[A-Z0-9]+).*?lev=(?P<lev>[0-9]+)')
RE_LEV_VER = re.compile(r'LEVERAGE_VERIFY.*?(?P<symbol>[A-Z0-9]+).*?(?:lev=)?(?P<lev>[0-9]+)')

def now_utc_iso():
    return datetime.now(timezone.utc).isoformat().replace("+00:00","Z")

def safe_float(x, default=None):
    try:
        return float(x)
    except Exception:
        return default

def safe_int(x, default=None):
    try:
        return int(float(x))
    except Exception:
        return default

def append_jsonl(obj: dict):
    obj = dict(obj)
    obj.setdefault("ts_ingest", now_utc_iso())
    with OUT_JSONL.open("a", encoding="utf-8") as f:
        f.write(json.dumps(obj, separators=(",", ":"), ensure_ascii=False) + "\n")

def save_state(state: dict):
    tmp = OUT_STATE.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    tmp.replace(OUT_STATE)

def tail_follow(path: Path, start_at_end=True):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.touch(exist_ok=True)
    f = path.open("r", encoding="utf-8", errors="replace")
    if start_at_end:
        f.seek(0, 2)
    while True:
        line = f.readline()
        if not line:
            time.sleep(SLEEP_SEC)
            continue
        yield line.rstrip("\n")

def parse_ts_line(line: str):
    m = RE_TS.match(line)
    if not m:
        return None, line.strip()
    return m.group("ts"), m.group("msg").strip()

def main():
    # Keep latest agent context per symbol and "global best"
    ctx = {
        "agent_latest_by_symbol": {},  # symbol -> {z, side, action, best, zbest, ts}
        "agent_latest_flat": None,     # last flat line
        "lev_by_symbol": {},           # symbol -> lev (from logs if available)
        "last_exec_open_by_symbol": {},# symbol -> last open event summary
        "started_at": now_utc_iso(),
    }
    save_state(ctx)

    # We follow both logs in a simple round-robin loop
    agent_iter = tail_follow(AGENT_LOG, start_at_end=False)
    exec_iter  = tail_follow(EXEC_LOG,  start_at_end=False)

    # To avoid blocking on one iterator, we read small bursts
    while True:
        # Burst from agent
        for _ in range(25):
            try:
                line = next(agent_iter)
            except Exception:
                break
            ts, msg = parse_ts_line(line)
            if not msg:
                continue

            m = RE_AGENT.match(msg)
            if m:
                action = m.group("action")
                side   = m.group("side")
                symbol = m.group("symbol")
                z      = safe_float(m.group("z"))
                best   = m.group("best")
                zbest  = safe_float(m.group("zbest"))
                ctx["agent_latest_by_symbol"][symbol] = {
                    "ts": ts, "action": action, "side": side, "symbol": symbol,
                    "z": z, "best": best, "zbest": zbest
                }
                append_jsonl({
                    "type": "agent_signal",
                    "ts": ts, "action": action, "side": side, "symbol": symbol,
                    "z": z, "best": best, "zbest": zbest
                })
                continue

            m = RE_AGENT_FLAT.match(msg)
            if m:
                ctx["agent_latest_flat"] = {"ts": ts, **m.groupdict()}
                append_jsonl({
                    "type": "agent_flat",
                    "ts": ts,
                    "reason": m.group("reason"),
                    "best": m.group("best"),
                    "z": safe_float(m.group("z"))
                })
                continue

        # Burst from executor
        for _ in range(50):
            try:
                line = next(exec_iter)
            except Exception:
                break
            ts, msg = parse_ts_line(line)
            if not msg:
                continue

            # leverage logs
            m = RE_LEV_SET.search(msg)
            if m:
                sym = m.group("symbol")
                lev = safe_int(m.group("lev"))
                if sym and lev:
                    ctx["lev_by_symbol"][sym] = lev
                append_jsonl({"type":"exec_leverage_set","ts":ts,"symbol":sym,"lev":lev,"raw":msg})
                continue

            m = RE_LEV_VER.search(msg)
            if m:
                sym = m.group("symbol")
                lev = safe_int(m.group("lev"))
                if sym and lev:
                    ctx["lev_by_symbol"][sym] = lev
                append_jsonl({"type":"exec_leverage_verify","ts":ts,"symbol":sym,"lev":lev,"raw":msg})
                continue

            # open
            m = RE_OPEN.match(msg)
            if m:
                sym = m.group("symbol")
                side = m.group("side")
                margin = safe_float(m.group("margin"), 0.0) or 0.0
                notional = safe_float(m.group("notional"), 0.0) or 0.0
                qty = safe_float(m.group("qty"))
                mark = safe_float(m.group("mark"))
                src = m.group("src") or None
                lev_est = (notional / margin) if margin > 0 else None

                agent_ctx = ctx["agent_latest_by_symbol"].get(sym, None)
                lev_log = ctx["lev_by_symbol"].get(sym, None)

                evt = {
                    "type":"exec_open",
                    "ts": ts,
                    "symbol": sym,
                    "side": side,
                    "qty": qty,
                    "mark": mark,
                    "margin": margin,
                    "notional": notional,
                    "lev_est": lev_est,
                    "lev_logged": lev_log,
                    "src": src,
                    "agent_ctx": agent_ctx,
                    "raw": msg
                }
                ctx["last_exec_open_by_symbol"][sym] = {
                    "ts": ts, "side": side, "qty": qty, "mark": mark,
                    "margin": margin, "notional": notional, "lev_est": lev_est,
                    "lev_logged": lev_log
                }
                append_jsonl(evt)
                continue

            # close
            m = RE_CLOSE.match(msg)
            if m:
                sym = m.group("symbol")
                side = m.group("side")
                qty = safe_float(m.group("qty"))
                mark = safe_float(m.group("mark"))
                last_open = ctx["last_exec_open_by_symbol"].get(sym)
                append_jsonl({
                    "type":"exec_close",
                    "ts": ts,
                    "symbol": sym,
                    "side": side,
                    "qty": qty,
                    "mark": mark,
                    "last_open": last_open,
                    "raw": msg
                })
                continue

            # tp hit
            m = RE_TP.match(msg)
            if m:
                sym = m.group("symbol")
                append_jsonl({
                    "type":"exec_tp_hit",
                    "ts": ts,
                    "symbol": sym,
                    "roi": safe_float(m.group("roi")),
                    "tp_thr": safe_float(m.group("tp_thr")),
                    "mode": m.group("mode"),
                    "vol": safe_float(m.group("vol")),
                    "last_open": ctx["last_exec_open_by_symbol"].get(sym),
                    "raw": msg
                })
                continue

            # sl hit (future-proof)
            m = RE_SL.match(msg)
            if m:
                sym = m.group("symbol")
                append_jsonl({
                    "type":"exec_sl_hit",
                    "ts": ts,
                    "symbol": sym,
                    "roi": safe_float(m.group("roi")),
                    "last_open": ctx["last_exec_open_by_symbol"].get(sym),
                    "raw": msg
                })
                continue

        # persist state occasionally
        save_state(ctx)
        time.sleep(SLEEP_SEC)

if __name__ == "__main__":
    main()
