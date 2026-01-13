#!/usr/bin/env bash
set -u
cd /root/bbotpat_live || exit 1

LOG="/root/bbotpat_live/fix_private_req_1021_v2_$(date +%Y%m%d_%H%M%S).log"
echo "Logging to: $LOG"
echo "If anything scrolls: tail -n 200 $LOG"

(
  umask 022

  echo "=== 0) Backup base ==="
  cp -a kc3_execute_futures.py "kc3_execute_futures.py.bak.$(date +%Y%m%d_%H%M%S)" || exit 1

  echo "=== 1) Patch base: insert time-sync helpers (if missing) + replace private_req() block ==="
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

HELP_MARK = "# --- KC3_TIME_SYNC_HARDEN_V2 ---"
if HELP_MARK not in s:
    helper = f"""
{HELP_MARK}
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
    if time.time() - _KC3_TIME_OFFSET_AT > 60:
        _kc3_refresh_time_offset()
    return int(time.time() * 1000) + int(_KC3_TIME_OFFSET_MS)

def _kc3_is_1021(text: str) -> bool:
    t = (text or "")
    return ("-1021" in t) or ("recvWindow" in t) or ("outside of the recvWindow" in t) or ("Timestamp for this request" in t)
"""
    # Insert helper after BASE_URL if present, else near top
    m = re.search(r'(?m)^BASE_URL\s*=', s)
    if m:
        line_end = s.find("\n", m.start())
        s = s[:line_end+1] + helper + s[line_end+1:]
    else:
        m2 = re.search(r'(?m)^(import .+\n)+', s)
        at = m2.end() if m2 else 0
        s = s[:at] + helper + s[at:]

# Replace private_req block
m = re.search(r'(?m)^def private_req\(', s)
if not m:
    raise SystemExit("Could not find def private_req(")

start = m.start()
m2 = re.search(r'(?m)^\S', s[start:])  # not used; keep clarity

# find next top-level def after private_req
m_next = re.search(r'(?m)^def\s+\w+\(', s[m.end():])
end = (m.end() + m_next.start()) if m_next else len(s)

replacement = r'''def private_req(method: str, path: str, params: dict):
    """
    Signed Binance Futures request hardened against -1021:
    - uses server-time offset timestamp
    - sets recvWindow (default 60000; override with KC3_RECV_WINDOW_MS)
    - retries once on -1021 after refreshing time offset
    """
    if params is None:
        params = {}

    # default recvWindow
    try:
        rw = int(float(os.getenv("KC3_RECV_WINDOW_MS", "60000") or "60000"))
    except Exception:
        rw = 60000
    if "recvWindow" not in params:
        params["recvWindow"] = rw

    # ensure we have a fresh offset occasionally
    _kc3_refresh_time_offset()

    headers = {"X-MBX-APIKEY": API_KEY}

    def _do():
        params["timestamp"] = _kc3_now_ms()
        qs = urlencode(params, doseq=True)
        sig = hmac.new(API_SECRET.encode(), qs.encode(), hashlib.sha256).hexdigest()
        params["signature"] = sig
        return requests.request(method, BASE_URL + path, headers=headers, params=params, timeout=10)

    r = _do()
    if r.status_code < 300:
        return r.json()

    txt = r.text
    if _kc3_is_1021(txt):
        # refresh time and retry once
        _kc3_refresh_time_offset()
        r2 = _do()
        if r2.status_code < 300:
            return r2.json()
        raise RuntimeError(f"{method} {path} failed {r2.status_code}: {r2.text}")

    raise RuntimeError(f"{method} {path} failed {r.status_code}: {r.text}")
'''

s2 = s[:start] + replacement + "\n\n" + s[end:]
p.write_text(s2, encoding="utf-8")
print("OK: inserted time-sync helpers (if needed) + replaced private_req() block safely.")
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
