#!/usr/bin/env bash
set -euo pipefail
cd /root/bbotpat_live || exit 1

echo "Stopping robust..."
pkill -f kc3_execute_futures_robust.py 2>/dev/null || true
sleep 1

echo "Patching base helpers safely..."
python3 - <<'PY'
from pathlib import Path
import re

p = Path("kc3_execute_futures.py")
s = p.read_text(errors="replace")

changed = False

if "def _kc3_is_1021" not in s:
    s += """

def _kc3_is_1021(e):
    msg = str(e)
    return "-1021" in msg or "recvWindow" in msg or "Timestamp" in msg
"""
    changed = True

if not re.search(r'def\\s+get_position\\(', s):
    raise SystemExit("ERROR: get_position truly missing – STOP")

p.write_text(s)
print("OK: helper compatibility restored:", changed)
PY

python3 -m py_compile kc3_execute_futures.py
echo "Base compiles clean"

echo "Restarting robust..."
nohup python3 kc3_execute_futures_robust.py >> kc3_exec.log 2>&1 &
sleep 2
tail -n 20 kc3_exec.log
