#!/usr/bin/env bash
cd /root/bbotpat_live || exit 1

LOG="/root/bbotpat_live/apply_dynamic_tp_safe_$(date +%Y%m%d_%H%M%S).log"
echo "Logging to: $LOG"
echo "If anything scrolls: tail -n 200 $LOG"
echo "---- START ----" | tee -a "$LOG"

# IMPORTANT: do NOT use set -e (that’s what kills your terminal on any error)
set -u
umask 022

echo "1) Stop executor only" | tee -a "$LOG"
pkill -f kc3_execute_futures_robust.py 2>/dev/null || true
sleep 1

echo "2) Backup executor + env" | tee -a "$LOG"
cp -a kc3_execute_futures_robust.py "kc3_execute_futures_robust.py.bak.$(date +%Y%m%d_%H%M%S)" 2>>"$LOG" || {
  echo "Backup failed (kc3_execute_futures_robust.py missing?)" | tee -a "$LOG"
  exit 1
}
cp -a .env ".env.bak.$(date +%Y%m%d_%H%M%S)" 2>>"$LOG" || true

echo "3) Ensure TP knobs exist in .env (does NOT touch leverage/SL)" | tee -a "$LOG"
sed -i \
  -e '/^KC3_TP_MODE=/d' \
  -e '/^KC3_TP_VOL_LOOKBACK_SEC=/d' \
  -e '/^KC3_TP_K=/d' \
  -e '/^KC3_TP_MIN=/d' \
  -e '/^KC3_TP_MAX=/d' \
  .env 2>>"$LOG" || true

cat >> .env <<'EOF'

# --- Dynamic TP (vol-based) ---
# Uses BTC-relative "rel" history from data/kc3_lag_state.json written by agent.
KC3_TP_MODE=vol
KC3_TP_VOL_LOOKBACK_SEC=21600
KC3_TP_K=1.8
KC3_TP_MIN=0.003
KC3_TP_MAX=0.012
EOF

echo "TP env now:" | tee -a "$LOG"
grep -E '^KC3_TP_' .env | tail -n 20 | tee -a "$LOG"

echo "4) Patch executor file (deterministic patch against real lines)" | tee -a "$LOG"
python3 - <<'PY' >>"$LOG" 2>&1
from pathlib import Path
import re

p = Path("kc3_execute_futures_robust.py")
s = p.read_text(encoding="utf-8", errors="replace").replace("\t","    ")

# Verify file has the block we saw (tp = TP_PCT)
if "tp = TP_PCT" not in s:
    raise SystemExit("Expected 'tp = TP_PCT' not found. File differs from what we saw.")

MARK = "# --- DYNAMIC_TP_HELPERS_FINAL ---"
if MARK not in s:
    # Insert helpers after SL_PCT line near top
    m = re.search(r"(?m)^\s*SL_PCT\s*=\s*float\(os\.getenv\(\"KC3_SL_PCT\"[^\n]*\)\)\s*$", s)
    if not m:
        raise SystemExit("Could not find SL_PCT env line to insert helpers after.")
    insert_at = m.end()

    helpers = f"""

{MARK}
import json, math
from pathlib import Path as _Path

def _clamp(x, lo, hi):
    return max(lo, min(hi, x))

def _read_lag_history():
    try:
        fp = _Path(__file__).resolve().parent / "data" / "kc3_lag_state.json"
        if not fp.exists():
            return []
        d = json.loads(fp.read_text(encoding="utf-8"))
        h = d.get("history") or []
        return h if isinstance(h, list) else []
    except Exception:
        return []

def _tok(symbol: str) -> str:
    if not symbol or not isinstance(symbol, str):
        return ""
    return symbol.replace("USDT","").upper().strip()

def dynamic_tp_threshold(symbol: str):
    mode = (os.getenv("KC3_TP_MODE","") or "").strip().lower()
    if mode != "vol":
        return TP_PCT, "fixed", None

    tok = _tok(symbol)
    if not tok:
        return TP_PCT, "fixed", None

    lookback_sec = float(os.getenv("KC3_TP_VOL_LOOKBACK_SEC","21600") or "21600")
    k = float(os.getenv("KC3_TP_K","1.8") or "1.8")
    tp_min = float(os.getenv("KC3_TP_MIN","0.003") or "0.003")
    tp_max = float(os.getenv("KC3_TP_MAX","0.012") or "0.012")

    hist = _read_lag_history()
    if len(hist) < 10:
        return TP_PCT, "fixed", None

    pts = max(20, int(lookback_sec / 15.0))  # agent loop ~15s
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
        return TP_PCT, "fixed", None

    mu = sum(vals)/len(vals)
    var = sum((x-mu)**2 for x in vals)/len(vals)
    vol = math.sqrt(var)

    tp = _clamp(k*vol, tp_min, tp_max)
    return tp, "vol", vol
"""
    s = s[:insert_at] + helpers + s[insert_at:]

# Replace the first occurrence of "tp = TP_PCT" in the TP block
s, n = re.subn(
    r"(?m)^(\s*)tp\s*=\s*TP_PCT\s*$",
    r"\1tp_thr, tp_mode, tp_vol = dynamic_tp_threshold(sym)\n"
    r"\1tp = tp_thr",
    s,
    count=1
)
if n != 1:
    raise SystemExit("Failed to replace 'tp = TP_PCT' exactly once.")

# Enhance TP hit log line (if present)
s = re.sub(
    r'print\(f"\[\{utc\(\)\}\]\s+TP hit \{sym\} roi=\{roi:\.4f\}"\, flush=True\)',
    'print(f"[{utc()}] TP hit {sym} roi={roi:.4f} tp_thr={tp_thr:.4f} mode={tp_mode} vol={tp_vol if tp_vol is not None else \'NA\'}", flush=True)',
    s,
    count=1
)

# Ensure TP condition uses tp variable (your file already compares roi>=tp)
s = re.sub(
    r"(?m)^(\s*)if\s+TP_PCT\s*>\s*0\s+and\s*\(\s*roi\s*>=\s*tp\s*\)\s*:\s*$",
    r"\1if tp > 0 and (roi >= tp):",
    s,
    count=1
)

p.write_text(s, encoding="utf-8")
print("OK: dynamic TP injected + TP block patched.")
PY

echo "5) Compile check" | tee -a "$LOG"
python3 -m py_compile kc3_execute_futures_robust.py >>"$LOG" 2>&1 || {
  echo "COMPILE FAILED. See: $LOG" | tee -a "$LOG"
  exit 1
}

echo "6) Start executor (env loaded)" | tee -a "$LOG"
set -a
source .env
set +a

nohup python3 kc3_execute_futures_robust.py > kc3_exec.log 2>&1 &
sleep 2

echo "Executor tail:" | tee -a "$LOG"
tail -n 30 kc3_exec.log | tee -a "$LOG"

echo "---- DONE ----" | tee -a "$LOG"
echo "Verify patch marker:" | tee -a "$LOG"
grep -n "DYNAMIC_TP_HELPERS_FINAL" kc3_execute_futures_robust.py | tee -a "$LOG" || true
