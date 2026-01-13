#!/usr/bin/env bash
set -u
cd /root/bbotpat_live || exit 1

LOG="/root/bbotpat_live/fix_1021_now_$(date +%Y%m%d_%H%M%S).log"
echo "Logging to: $LOG"
echo "If anything scrolls: tail -n 200 $LOG"

(
  umask 022

  echo "=== 0) Stop robust (so we can patch safely) ==="
  pkill -f kc3_execute_futures_robust.py 2>/dev/null || true
  sleep 1
  pgrep -af kc3_execute_futures_robust.py || echo "robust stopped"

  echo "=== 1) Backup current base file (even if broken) ==="
  cp -a kc3_execute_futures.py "kc3_execute_futures.py.bak.BROKEN_$(date +%Y%m%d_%H%M%S)" || true

  echo "=== 2) Restore the newest backup of kc3_execute_futures.py that COMPILES ==="
  python3 - <<'PY'
import glob, subprocess, sys, shutil

cands = sorted(glob.glob("kc3_execute_futures.py.bak.*"), reverse=True)
if not cands:
    print("NO BACKUPS FOUND: kc3_execute_futures.py.bak.*")
    raise SystemExit(2)

ok=None
for f in cands:
    try:
        subprocess.check_call([sys.executable, "-m", "py_compile", f],
                              stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        ok=f
        break
    except Exception:
        continue

if not ok:
    print("FOUND BACKUPS BUT NONE COMPILE")
    raise SystemExit(3)

shutil.copy2(ok, "kc3_execute_futures.py")
print("RESTORED_FROM:", ok)
PY

  echo "=== 3) Apply time sync + -1021 retry patch (idempotent) ==="
  python3 - <<'PY'
from pathlib import Path
import re

p = Path("kc3_execute_futures.py")
s = p.read_text(encoding="utf-8", errors="replace").replace("\t","    ")

# Ensure imports
if not re.search(r'(?m)^\s*import\s+time\s*$', s):
    m = re.search(r'(?m)^(import|from)\s+.+\n', s)
    if m:
        s = s[:m.end()] + "import time\n" + s[m.end():]
    else:
        s = "import time\n" + s

# Insert/replace KC3_TIME_SYNC block right before def private_req(
priv_m = re.search(r'(?m)^def\s+private_req\(', s)
if not priv_m:
    raise SystemExit("Could not find def private_req(")

mark = "# --- KC3_TIME_SYNC ---"
block = """
# --- KC3_TIME_SYNC ---
_KC3_TIME_OFFSET_MS = 0
_KC3_TIME_OFFSET_TS = 0.0

def _kc3_now_ms() -> int:
    return int(time.time() * 1000)

def _kc3_refresh_time_offset_ms(force: bool = False) -> int:
    global _KC3_TIME_OFFSET_MS, _KC3_TIME_OFFSET_TS
    # refresh at most every 60s unless forced
    if (not force) and (_KC3_TIME_OFFSET_TS > 0) and (time.time() - _KC3_TIME_OFFSET_TS < 60):
        return int(_KC3_TIME_OFFSET_MS)
    try:
        d = requests.get(BASE_URL + "/fapi/v1/time", timeout=5).json()
        server = int(d.get("serverTime"))
        local = _kc3_now_ms()
        _KC3_TIME_OFFSET_MS = server - local
        _KC3_TIME_OFFSET_TS = time.time()
        return int(_KC3_TIME_OFFSET_MS)
    except Exception:
        return int(_KC3_TIME_OFFSET_MS)

def _kc3_signed_timestamp_ms() -> int:
    off = _kc3_refresh_time_offset_ms(force=False)
    return _kc3_now_ms() + int(off)

def _kc3_is_timestamp_error(text: str) -> bool:
    return ('"code":-1021' in text) or ("'code': -1021" in text) or ("outside of the recvWindow" in text)
"""

# Remove any old KC3_TIME_SYNC block if present (from marker up to private_req)
if mark in s:
    i0 = s.find(mark)
    i1 = priv_m.start()
    s = s[:i0] + s[i1:]

# Re-find private_req after removal
priv_m = re.search(r'(?m)^def\s+private_req\(', s)
ins = priv_m.start()
s = s[:ins] + block + "\n" + s[ins:]

# Patch private_req to use signed timestamp and retry on -1021 once
# We look for a place where timestamp is set, otherwise we inject into signed-request branch.
# Strategy:
# 1) If we see "params['timestamp']" or "params[\"timestamp\"]", replace its value assignment.
s = re.sub(r'(?m)^(?P<ind>\s*)params\[(\'|")timestamp(\\2)\]\s*=\s*.*$',
           r'\g<ind>params["timestamp"] = _kc3_signed_timestamp_ms()',
           s)

# 2) Ensure there is a retry wrapper around the HTTP request inside private_req
# We'll patch by finding the line that does the request (requests.request(...)) and wrapping minimal.
# This patch is conservative: if already patched, it won't double-wrap.
if "KC3_RETRY_1021" not in s:
    # Find the first occurrence of "requests.request(" inside private_req
    m = re.search(r'(?s)(def\s+private_req\(.*?\):\n)(.*?)(\n\s*return\s+.*?\n)', s)
    # Not strictly needed. We'll do a targeted insertion near the requests.request call.
    rr = re.search(r'(?m)^(?P<ind>\s*)r\s*=\s*requests\.request\(', s)
    if rr:
        ind = rr.group("ind")
        # Insert a simple retry loop just above the "r = requests.request(" line
        insert = (
            f"{ind}# --- KC3_RETRY_1021 ---\n"
            f"{ind}for _kc3_try in (1, 2):\n"
            f"{ind}    if _kc3_try == 2:\n"
            f"{ind}        _kc3_refresh_time_offset_ms(force=True)\n"
        )
        # Then indent the original request line + subsequent immediate lines that reference r before status check
        # We will indent only the single request assignment line; the rest of code stays same.
        s = s[:rr.start()] + insert + ind + "    " + s[rr.start():]
        # After request, if it fails with -1021, continue loop, else break.
        # Find where r.text is used in an error or where status_code checked; inject right after request line.
        rr2 = re.search(r'(?m)^(?P<ind>\s*)for _kc3_try in \(1, 2\):\n.*?\n(?P<reqind>\s*)r\s*=\s*requests\.request\([^\n]*\)\s*$',
                        s)
        if rr2:
            reqind = rr2.group("reqind")
            # Insert check just after the request line (same indentation as reqind)
            # We'll locate end of that request line.
            req_line_end = s.find("\n", rr2.end())
            if req_line_end != -1:
                after = (
                    f"{reqind}if (r is not None) and (getattr(r,'status_code',0) == 400) and _kc3_is_timestamp_error(getattr(r,'text','') or ''):\n"
                    f"{reqind}    if _kc3_try == 1:\n"
                    f"{reqind}        continue\n"
                    f"{reqind}# ok (or non-1021) -> break retry loop\n"
                    f"{reqind}break\n"
                )
                s = s[:req_line_end+1] + after + s[req_line_end+1:]

# Write back
p.write_text(s, encoding="utf-8")
print("OK: time sync + -1021 retry patch applied")
PY

  echo "=== 4) Compile check ==="
  python3 -m py_compile kc3_execute_futures.py || exit 1
  echo "OK: kc3_execute_futures.py compiles"

  echo "=== 5) Start robust again ==="
  set -a; source .env; set +a
  nohup python3 kc3_execute_futures_robust.py >> kc3_exec.log 2>&1 &
  sleep 2
  pgrep -af kc3_execute_futures_robust.py || echo "ROBUST NOT RUNNING"

  echo "=== 6) Verify -1021 stops happening ==="
  tail -n 60 kc3_exec.log
  echo
  echo "Recent -1021 lines:"
  grep -nE 'code":-1021|outside of the recvWindow' kc3_exec.log | tail -n 10 || echo "OK: none"

  echo "=== DONE ==="
) 2>&1 | tee "$LOG"
