#!/usr/bin/env bash
set -e
cd /root/bbotpat_live || exit 1

python3 - <<'PY'
from pathlib import Path
import re

p = Path("kc3_execute_futures.py")
s = p.read_text(errors="replace")

# Replace any timestamp assignment inside private_req
def repl(match):
    block = match.group(0)
    block = re.sub(
        r'params\["timestamp"\]\s*=\s*.*',
        'params["timestamp"] = int(time.time() * 1000)\n        params["recvWindow"] = 5000',
        block
    )
    return block

s_new, n = re.subn(
    r'def private_req\([\s\S]*?\n\s*return\s+.*',
    repl,
    s,
    count=1
)

if n != 1:
    raise SystemExit("FAILED: could not patch private_req safely")

p.write_text(s_new)
print("OK: timestamp logic patched safely")
PY

python3 -m py_compile kc3_execute_futures.py && echo "COMPILE OK"
