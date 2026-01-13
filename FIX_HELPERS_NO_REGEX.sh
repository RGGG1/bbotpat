#!/usr/bin/env bash
set -euo pipefail
cd /root/bbotpat_live || exit 1

echo "=== Stopping robust safely ==="
pkill -f kc3_execute_futures_robust.py 2>/dev/null || true
sleep 1

echo "=== Patching helpers (NO REGEX, SAFE APPEND ONLY) ==="
python3 - <<'PY'
from pathlib import Path

p = Path("kc3_execute_futures.py")
s = p.read_text(errors="replace")

# Hard safety check
if "def get_position" not in s:
    raise SystemExit("FATAL: get_position is missing from base file. STOP.")

changed = False

if "def _kc3_is_1021" not in s:
    s += """

def _kc3_is_1021(e):
    msg = str(e)
    return ("-1021" in msg) or ("recvWindow" in msg) or ("Timestamp" in msg)
"""
    changed = True

p.write_text(s)
print("Helpers patched:", changed)
PY

echo "=== Compile check ==="
python3 -m py_compile kc3_execute_futures.py

echo "=== Restarting robust ==="
nohup python3 kc3_execute_futures_robust.py >> kc3_exec.log 2>&1 &
sleep 2

echo "=== Log tail ==="
tail -n 30 kc3_exec.log
