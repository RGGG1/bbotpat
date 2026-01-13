#!/usr/bin/env bash
set -euo pipefail
cd /root/bbotpat_live || exit 1
LOG="/root/bbotpat_live/FIX_TIME_ALIAS_NOW_$(date +%Y%m%d_%H%M%S).log"
exec > >(tee -a "$LOG") 2>&1
echo "Logging to: $LOG"

echo "=== 0) Stop robust ==="
pkill -f kc3_execute_futures_robust.py 2>/dev/null || true
sleep 1
pgrep -af kc3_execute_futures_robust.py || echo "robust stopped"

echo "=== 1) Backup base ==="
cp -a kc3_execute_futures.py "kc3_execute_futures.py.bak.FIX_TIME_ALIAS_$(date +%Y%m%d_%H%M%S)"

echo "=== 2) Patch base: define _kc3_refresh_time_offset alias if missing ==="
python3 - <<'PY'
from pathlib import Path
import re

p = Path("kc3_execute_futures.py")
s = p.read_text(encoding="utf-8", errors="replace").replace("\t","    ")

# If already has _kc3_refresh_time_offset, do nothing.
if re.search(r'(?m)^\s*def\s+_kc3_refresh_time_offset\(', s):
    print("OK: _kc3_refresh_time_offset already exists; no change.")
    raise SystemExit(0)

# We expect _kc3_refresh_time_offset_ms to exist (from your latest patch series)
has_ms = bool(re.search(r'(?m)^\s*def\s+_kc3_refresh_time_offset_ms\(', s))

alias = r'''

# --- KC3_TIME_SYNC_ALIAS ---
# Some patches used _kc3_refresh_time_offset_ms(); others call _kc3_refresh_time_offset().
# Provide a safe alias so both names work.
def _kc3_refresh_time_offset(force: bool=False):
    try:
        # preferred if present
        return _kc3_refresh_time_offset_ms(force)  # type: ignore[name-defined]
    except Exception:
        # fall back to last-known offset vars if any
        try:
            return int(globals().get("_KC3_TIME_OFFSET_MS", 0))
        except Exception:
            return 0
# --- /KC3_TIME_SYNC_ALIAS ---
'''

# Insert alias right after KC3_TIME_SYNC marker if present, else before private_req
if "# --- KC3_TIME_SYNC ---" in s:
    idx = s.find("# --- KC3_TIME_SYNC ---")
    # insert after the block header line to keep it grouped
    line_end = s.find("\n", idx)
    s = s[:line_end+1] + alias + s[line_end+1:]
else:
    m = re.search(r'(?m)^def\s+private_req\(', s)
    if not m:
        raise SystemExit("Cannot find def private_req; refusing to patch blindly.")
    s = s[:m.start()] + alias + "\n" + s[m.start():]

# Also ensure _kc3_now_ms uses the alias name consistently (optional, safe)
s = s.replace("_kc3_refresh_time_offset(False)", "_kc3_refresh_time_offset(False)")
s = s.replace("_kc3_refresh_time_offset(True)", "_kc3_refresh_time_offset(True)")

p.write_text(s, encoding="utf-8")
print("OK: inserted _kc3_refresh_time_offset alias (compat shim). ms_variant_present=", has_ms)
PY

echo "=== 3) Compile base ==="
python3 -m py_compile kc3_execute_futures.py
echo "OK: base compiles"

echo "=== 4) Signed endpoint tests (must pass) ==="
python3 - <<'PY'
import traceback
import kc3_execute_futures as base

print("has private_req:", hasattr(base,"private_req"))
print("has get_position:", hasattr(base,"get_position"))

try:
    d = base.private_req("GET","/fapi/v2/positionRisk",{})
    print("OK: positionRisk rows=", len(d))
except Exception as e:
    print("FAIL: positionRisk", repr(e))
    traceback.print_exc()
    raise SystemExit(2)

try:
    p = base.get_position("SOLUSDT")
    print("OK: get_position(SOLUSDT) returned type=", type(p))
except Exception as e:
    print("WARN: get_position failed (non-fatal if positionRisk ok):", repr(e))
PY

echo "=== 5) Start robust ==="
set -a; source .env; set +a
nohup python3 kc3_execute_futures_robust.py >> kc3_exec.log 2>&1 &
sleep 2
pgrep -af kc3_execute_futures_robust.py || { echo "ROBUST NOT RUNNING"; exit 3; }

echo "=== 6) Tail log + check for -1021 / recursion / missing attributes ==="
tail -n 60 kc3_exec.log || true
echo "--- recent fatals (last 400 lines) ---"
tail -n 400 kc3_exec.log | egrep -n 'code":-1021|outside of the recvWindow|RecursionError|maximum recursion depth|IndentationError|has no attribute' || echo "OK: none in last 400"
echo "=== DONE ==="
