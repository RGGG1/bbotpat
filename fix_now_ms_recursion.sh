#!/usr/bin/env bash
set -u
cd /root/bbotpat_live || exit 1

LOG="/root/bbotpat_live/fix_now_ms_recursion_$(date +%Y%m%d_%H%M%S).log"
echo "Logging to: $LOG"
echo "If anything scrolls: tail -n 200 $LOG"

(
  umask 022

  echo "=== 0) Stop robust (safe) ==="
  pkill -f kc3_execute_futures_robust.py 2>/dev/null || true
  sleep 1
  pgrep -af kc3_execute_futures_robust.py || echo "robust stopped"

  echo "=== 1) Backup base ==="
  cp -a kc3_execute_futures.py "kc3_execute_futures.py.bak.$(date +%Y%m%d_%H%M%S)" || exit 1

  echo "=== 2) Patch _kc3_now_ms() to non-recursive implementation ==="
  python3 - <<'PY'
from pathlib import Path
import re

p = Path("kc3_execute_futures.py")
s = p.read_text(encoding="utf-8", errors="replace").replace("\t","    ")

# ensure `import time` exists
if not re.search(r'(?m)^\s*import\s+time\s*$', s):
    m = re.search(r'(?m)^(import|from)\s+.+\n', s)
    if m:
        s = s[:m.end()] + "import time\n" + s[m.end():]
    else:
        s = "import time\n" + s

# Replace the entire def _kc3_now_ms(...) block
pat = r'(?ms)^def _kc3_now_ms\(\)\s*:\s*\n(?:^[ \t].*\n)+'
m = re.search(pat, s)
if not m:
    raise SystemExit("ERROR: could not find def _kc3_now_ms() block to replace.")

replacement = (
"def _kc3_now_ms() -> int:\n"
"    \"\"\"Return current epoch ms adjusted by any server time offset.\n"
"    IMPORTANT: must never call itself.\n"
"    \"\"\"\n"
"    try:\n"
"        off = int(globals().get('_KC3_TIME_OFFSET_MS', 0) or 0)\n"
"    except Exception:\n"
"        off = 0\n"
"    return int(time.time() * 1000) + off\n"
"\n"
"# --- KC3_FIX_NOW_MS_RECURSION (marker) ---\n"
)

s2 = s[:m.start()] + replacement + s[m.end():]
p.write_text(s2, encoding="utf-8")
print("OK: replaced _kc3_now_ms() with safe non-recursive version.")
PY

  echo "=== 3) Compile check base ==="
  python3 -m py_compile kc3_execute_futures.py && echo "OK: base compiles"

  echo "=== 4) Quick signed endpoint test ==="
  python3 - <<'PY'
import kc3_execute_futures as base
d = base.private_req("GET","/fapi/v2/positionRisk",{})
print("OK: positionRisk rows =", len(d))
PY

  echo "=== 5) Start robust again ==="
  set -a; source .env; set +a
  nohup python3 kc3_execute_futures_robust.py >> kc3_exec.log 2>&1 &
  sleep 2
  pgrep -af kc3_execute_futures_robust.py || echo "ROBUST NOT RUNNING"

  echo "=== 6) Confirm recursion error gone from log tail ==="
  tail -n 30 kc3_exec.log | egrep -n "RecursionError|maximum recursion depth" || echo "OK: no recursion in last 30 lines"

) 2>&1 | tee "$LOG"
