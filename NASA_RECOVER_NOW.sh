#!/usr/bin/env bash
set -euo pipefail
cd /root/bbotpat_live || exit 1

LOG="/root/bbotpat_live/NASA_RECOVER_NOW_$(date +%Y%m%d_%H%M%S).log"
exec > >(tee -a "$LOG") 2>&1
echo "Logging to: $LOG"

echo "=== 0) Stop robust ==="
pkill -f kc3_execute_futures_robust.py 2>/dev/null || true
sleep 1
pgrep -af kc3_execute_futures_robust.py || echo "robust stopped"

echo "=== 1) Choose BEST base backup (newest SAME-DAY, has dynamic leverage, no now_ms recursion) ==="
python3 - <<'PY'
import glob, re, os, subprocess, sys

def ok(path: str) -> bool:
    s=open(path,'r',errors='replace').read()
    # must have core funcs
    if not re.search(r'(?m)^def\s+private_req\(', s): return False
    if not re.search(r'(?m)^def\s+get_position\(', s): return False
    if not re.search(r'(?m)^def\s+open_position\(', s): return False
    # must have dynamic leverage evidence
    if ("KC3_LEVERAGE_DECISION" not in s) and ("used_for_size" not in s) and ("KC3_LEV_MODE" not in s):
        return False
    # must NOT contain the recursion bug pattern
    if re.search(r'(?m)^\s*def\s+_kc3_now_ms\(\)\s*:\s*$', s):
        # if the function body contains "return _kc3_now_ms()" anywhere, it's broken
        if "return _kc3_now_ms()" in s:
            return False
    # must compile as a module when swapped in
    try:
        subprocess.check_call([sys.executable,"-m","py_compile",path], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception:
        return False
    return True

# Prefer ONLY today’s backups (20260113) and not BROKEN tags unless nothing else exists.
cands = sorted(glob.glob("kc3_execute_futures.py.bak.*"), reverse=True)
today = [c for c in cands if "20260113" in c and ".bak.BROKEN_" not in c]
fallback = [c for c in cands if "20260113" in c]  # includes BROKEN if needed

best = None
for pool in (today, fallback, cands):
    for f in pool:
        if ok(f):
            best = f
            break
    if best: break

print("BEST_BASE_BACKUP =", best)
if not best:
    raise SystemExit("No suitable base backup found that matches criteria.")
open("/tmp/BEST_BASE_BACKUP.txt","w").write(best)
PY

BEST="$(cat /tmp/BEST_BASE_BACKUP.txt)"
echo "Selected: $BEST"

echo "=== 2) Restore base from BEST (this is SAME-DAY and includes dynamic leverage) ==="
cp -a kc3_execute_futures.py "kc3_execute_futures.py.preNASA.$(date +%Y%m%d_%H%M%S)" || true
cp -a "$BEST" kc3_execute_futures.py

echo "=== 3) Ensure minimal time-sync / -1021 hardening exists in base (idempotent) ==="
python3 - <<'PY'
from pathlib import Path
import re

p=Path("kc3_execute_futures.py")
s=p.read_text(encoding="utf-8", errors="replace").replace("\t","    ")

# Ensure `import time`
if not re.search(r'(?m)^\s*import\s+time\s*$', s):
    m=re.search(r'(?m)^(import|from)\s+.+\n', s)
    if m:
        s=s[:m.end()]+"import time\n"+s[m.end():]
    else:
        s="import time\n"+s

# If our time sync marker already present, do nothing.
if "# --- KC3_TIME_SYNC ---" not in s:
    # Insert helpers right before private_req
    m=re.search(r'(?m)^def\s+private_req\(', s)
    if not m:
        raise SystemExit("private_req not found; refusing to patch.")
    ins = r'''
# --- KC3_TIME_SYNC ---
_KC3_TIME_OFFSET_MS = 0
_KC3_TIME_OFFSET_SET_AT = 0.0

def _kc3_public_time_ms():
    # futures time endpoint (no auth)
    try:
        r = requests.get(BASE_URL + "/fapi/v1/time", timeout=5)
        j = r.json()
        return int(j.get("serverTime"))
    except Exception:
        return None

def _kc3_refresh_time_offset(force=False):
    global _KC3_TIME_OFFSET_MS, _KC3_TIME_OFFSET_SET_AT
    now = time.time()
    if (not force) and (now - _KC3_TIME_OFFSET_SET_AT) < 30:
        return _KC3_TIME_OFFSET_MS
    st = _kc3_public_time_ms()
    if st is None:
        return _KC3_TIME_OFFSET_MS
    local = int(time.time()*1000)
    _KC3_TIME_OFFSET_MS = int(st - local)
    _KC3_TIME_OFFSET_SET_AT = now
    return _KC3_TIME_OFFSET_MS

def _kc3_now_ms():
    # non-recursive, always safe
    return int(time.time()*1000) + int(_kc3_refresh_time_offset(False))
# --- /KC3_TIME_SYNC ---
'''
    s = s[:m.start()] + ins + "\n" + s[m.start():]

# Patch private_req to use _kc3_now_ms and retry once on -1021.
# Do NOT rewrite entire function; only minimal string edits if needed.
# 1) ensure timestamp assignment uses _kc3_now_ms()
s = re.sub(r'(?m)^\s*params\["timestamp"\]\s*=\s*int\(time\.time\(\)\s*\*\s*1000\)\s*$',
           '    params["timestamp"] = _kc3_now_ms()',
           s)

# 2) ensure recvWindow exists (Binance recommends; reduces -1021 sensitivity)
if 'recvWindow' not in s:
    # insert near timestamp usage inside private_req by adding a safe default assignment after params dict is built
    s = re.sub(r'(?m)^(\s*params\s*=\s*dict\([^\n]*\)\s*)$',
               r'\1\n    params.setdefault("recvWindow", 5000)',
               s)

# 3) add -1021 retry wrapper if missing (look for our marker)
if "KC3_1021_RETRY" not in s:
    # locate start of private_req body and wrap its request call with a retry
    m = re.search(r'(?ms)^def\s+private_req\([^\)]*\):\n(.*?)\n\s*(r\s*=\s*requests\.[a-z]+\()', s)
    if not m:
        # fallback: do nothing if we can't safely locate
        pass
    else:
        # We'll inject a small retry around the first actual request call within private_req.
        s = s.replace(m.group(2), m.group(2) + "\n    # --- KC3_1021_RETRY ---\n    _kc3_refresh_time_offset(force=True)\n")

# Also make sure we don't have the old recursion bug lingering
s = s.replace("return _kc3_now_ms()", "return int(time.time()*1000) + int(_kc3_refresh_time_offset(False))")

p.write_text(s, encoding="utf-8")
print("OK: base has non-recursive _kc3_now_ms + time offset helpers + mild -1021 hardening (or left unchanged if already present).")
PY

echo "=== 4) Compile base ==="
python3 -m py_compile kc3_execute_futures.py
echo "OK: base compiles"

echo "=== 5) Sanity test base functions + signed endpoint ==="
python3 - <<'PY'
import traceback
import kc3_execute_futures as base

print("has private_req:", hasattr(base,"private_req"))
print("has get_position:", hasattr(base,"get_position"))
if not hasattr(base,"private_req") or not hasattr(base,"get_position"):
    raise SystemExit("BASE MODULE IS MISSING REQUIRED FUNCTIONS AFTER RESTORE. STOP.")

try:
    d = base.private_req("GET","/fapi/v2/positionRisk",{})
    print("OK: positionRisk rows =", len(d))
except Exception as e:
    print("FAIL positionRisk:", repr(e))
    traceback.print_exc()
    raise

try:
    p = base.get_position("SOLUSDT")
    print("OK: get_position(SOLUSDT) returned:", type(p), p if isinstance(p,dict) else "")
except Exception as e:
    print("WARN get_position failed:", repr(e))
PY

echo "=== 6) Ensure robust compiles; if not, restore newest compiling SAME-DAY robust backup ==="
python3 - <<'PY'
import glob, subprocess, sys, re

def compiles(f):
    try:
        subprocess.check_call([sys.executable,"-m","py_compile",f], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return True
    except Exception:
        return False

if compiles("kc3_execute_futures_robust.py"):
    print("OK: current robust compiles")
    raise SystemExit(0)

cands = sorted(glob.glob("kc3_execute_futures_robust.py.bak.*"), reverse=True)
today = [c for c in cands if "20260113" in c and ".bak.BROKEN_" not in c]
best=None
for pool in (today, cands):
    for f in pool:
        if compiles(f):
            # prefer ones that include TP hit logic
            s=open(f,'r',errors='replace').read()
            if ("TP hit" in s) or ("dynamic_tp_threshold" in s):
                best=f
                break
    if best: break

print("RESTORE_ROBUST_FROM =", best)
if not best:
    raise SystemExit("No compiling robust backup found.")
import shutil
shutil.copy2("kc3_execute_futures_robust.py", "kc3_execute_futures_robust.py.preNASA")
shutil.copy2(best, "kc3_execute_futures_robust.py")
PY

echo "=== 7) Start robust ==="
set -a; source .env; set +a
nohup python3 kc3_execute_futures_robust.py >> kc3_exec.log 2>&1 &
sleep 2
pgrep -af kc3_execute_futures_robust.py || { echo "ROBUST NOT RUNNING"; exit 1; }

echo "=== 8) Quick health checks ==="
echo "--- last 40 log lines ---"
tail -n 40 kc3_exec.log || true

echo "--- recent fatal signatures ---"
tail -n 400 kc3_exec.log | egrep -n 'RecursionError|maximum recursion depth|IndentationError|AttributeError: module .* has no attribute|get_position failed 400:.*-1021' || echo "OK: none of the common fatals in last 400"

echo "=== DONE ==="
