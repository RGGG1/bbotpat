#!/usr/bin/env bash
set -u
cd /root/bbotpat_live || exit 1

LOG="/root/bbotpat_live/patch_private_req_1021_harden_$(date +%Y%m%d_%H%M%S).log"
echo "Logging to: $LOG"
echo "If anything scrolls: tail -n 200 $LOG"

(
  umask 022

  echo "=== 0) Backup base ==="
  cp -a kc3_execute_futures.py "kc3_execute_futures.py.bak.$(date +%Y%m%d_%H%M%S)" || exit 1

  echo "=== 1) Patch base: time offset + -1021 retry in private_req (surgical) ==="
  python3 - <<'PY'
from pathlib import Path
import re

p = Path("kc3_execute_futures.py")
s = p.read_text(encoding="utf-8", errors="replace").replace("\t","    ")

# Ensure import time exists
if not re.search(r'(?m)^\s*import\s+time\s*$', s):
    m = re.search(r'(?m)^(import|from)\s+.+\n', s)
    if m:
        s = s[:m.end()] + "import time\n" + s[m.end():]
    else:
        s = "import time\n" + s

# Inject globals + helper (idempotent)
if "_KC3_TIME_OFFSET_MS" not in s:
    inject = """
# --- KC3_TIME_SYNC_HARDEN ---
_KC3_TIME_OFFSET_MS = 0
_KC3_TIME_OFFSET_AT = 0.0

def _kc3_refresh_time_offset():
    global _KC3_TIME_OFFSET_MS, _KC3_TIME_OFFSET_AT
    try:
        r = requests.get(BASE_URL + "/fapi/v1/time", timeout=5)
        j = r.json()
        server_ms = int(j.get("serverTime"))
        local_ms = int(time.time() * 1000)
        _KC3_TIME_OFFSET_MS = server_ms - local_ms
        _KC3_TIME_OFFSET_AT = time.time()
        return _KC3_TIME_OFFSET_MS
    except Exception:
        return _KC3_TIME_OFFSET_MS

def _kc3_now_ms():
    # refresh at most every 60s
    if time.time() - _KC3_TIME_OFFSET_AT > 60:
        _kc3_refresh_time_offset()
    return int(time.time() * 1000) + int(_KC3_TIME_OFFSET_MS)

def _kc3_is_1021(msg: str) -> bool:
    return ("-1021" in (msg or "")) or ("recvWindow" in (msg or "")) or ("Timestamp for this request" in (msg or ""))
"""
    # Put after BASE_URL if present, else after imports
    m = re.search(r'(?m)^BASE_URL\s*=', s)
    if m:
        line_end = s.find("\n", m.start())
        s = s[:line_end+1] + inject + s[line_end+1:]
    else:
        m2 = re.search(r'(?m)^(import .+\n)+', s)
        at = m2.end() if m2 else 0
        s = s[:at] + inject + s[at:]

# Patch private_req internals: ensure params['timestamp']=_kc3_now_ms() and recvWindow, and retry on -1021
m = re.search(r'(?m)^def private_req\(', s)
if not m:
    raise SystemExit("Could not find def private_req(")

# Find where params['timestamp'] is set; if not found, we insert right before signing.
# We also ensure recvWindow exists.
# We'll do a conservative replace of "int(time.time()*1000)" if present.
s = re.sub(r'int\(time\.time\(\)\s*\*\s*1000\)', '_kc3_now_ms()', s)

# Ensure recvWindow is set in private_req when signing requests
# Add a small block near the start of private_req after params is created/seen.
priv_start = m.start()
priv_body_start = s.find(":", priv_start) + 1
# find first line after def private_req(...)
nl = s.find("\n", priv_body_start)
indent = re.match(r'^(\s*)', s[nl+1:]).group(1) if nl != -1 else "    "

marker = "# --- KC3_PRIVATE_REQ_HARDEN ---"
if marker not in s:
    hard = f"""
{indent}{marker}
{indent}# Increase recvWindow to reduce -1021 risk; Binance max is typically 60000
{indent}try:
{indent}    _rw = int(float(os.getenv("KC3_RECV_WINDOW_MS", "60000") or "60000"))
{indent}except Exception:
{indent}    _rw = 60000
{indent}if isinstance(params, dict) and "recvWindow" not in params:
{indent}    params["recvWindow"] = _rw
"""
    # Insert after "def private_req(...):" line
    def_line_end = s.find("\n", priv_start)
    s = s[:def_line_end+1] + hard + s[def_line_end+1:]

# Add retry wrapper around the request execution: we look for the final raise RuntimeError(...) in private_req
# and replace with: on -1021 refresh offset + retry once.
if "KC3_1021_RETRY" not in s:
    # Replace the line that raises RuntimeError(f"... failed ...")
    s = re.sub(
        r'raise RuntimeError\(f"\{method\} \{path\} failed \{r\.status_code\}: \{r\.text\}"\)',
        'msg = r.text\n        # --- KC3_1021_RETRY ---\n        if _kc3_is_1021(msg):\n            _kc3_refresh_time_offset()\n            # retry once with fresh time\n            params["timestamp"] = _kc3_now_ms()\n            qs = urlencode(params, doseq=True)\n            sig = hmac.new(API_SECRET.encode(), qs.encode(), hashlib.sha256).hexdigest()\n            params["signature"] = sig\n            r2 = requests.request(method, BASE_URL + path, headers=headers, params=params, timeout=10)\n            if r2.status_code < 300:\n                return r2.json()\n            raise RuntimeError(f"{method} {path} failed {r2.status_code}: {r2.text}")\n        raise RuntimeError(f"{method} {path} failed {r.status_code}: {r.text}")',
        s
    )

p.write_text(s, encoding="utf-8")
print("OK: private_req hardened for -1021 with time offset + recvWindow + retry.")
PY

  echo "=== 2) Compile base ==="
  python3 -m py_compile kc3_execute_futures.py
  echo "OK: base compiles"

  echo "=== 3) Restart robust ==="
  pkill -f kc3_execute_futures_robust.py 2>/dev/null || true
  sleep 1
  set -a; source .env; set +a
  nohup python3 kc3_execute_futures_robust.py >> kc3_exec.log 2>&1 &
  sleep 2
  pgrep -af kc3_execute_futures_robust.py || echo "ROBUST NOT RUNNING"

) 2>&1 | tee "$LOG"
