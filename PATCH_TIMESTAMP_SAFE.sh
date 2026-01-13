#!/usr/bin/env bash
set -e
cd /root/bbotpat_live || exit 1

python3 - <<'PY'
from pathlib import Path
import re

p = Path("kc3_execute_futures.py")
lines = p.read_text(errors="replace").splitlines()

out = []
inside = False
indent = ""

for line in lines:
    if line.startswith("def private_req"):
        inside = True
    if inside and "params[\"timestamp\"]" in line:
        indent = re.match(r'^(\s*)', line).group(1)
        out.append(f"{indent}params[\"timestamp\"] = int(time.time() * 1000)")
        out.append(f"{indent}params[\"recvWindow\"] = 5000")
        continue
    if inside and line.strip().startswith("return "):
        inside = False
    out.append(line)

p.write_text("\n".join(out))
print("OK: timestamp patched with correct indentation")
PY

python3 -m py_compile kc3_execute_futures.py && echo "COMPILE OK"
