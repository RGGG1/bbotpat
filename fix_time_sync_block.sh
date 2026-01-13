#!/usr/bin/env bash
cd /root/bbotpat_live || exit 1
LOG="/root/bbotpat_live/fix_time_sync_block_$(date +%Y%m%d_%H%M%S).log"
echo "Logging to: $LOG"
echo "If anything scrolls: tail -n 200 $LOG"

(
  set -u
  umask 022

  echo "=== 0) Backup ==="
  cp -a kc3_execute_futures.py "kc3_execute_futures.py.bak.$(date +%Y%m%d_%H%M%S)" || exit 1

  echo "=== 1) Replace KC3_TIME_SYNC block with known-good version ==="
  python3 - <<'PY'
from pathlib import Path
import re

p = Path("kc3_execute_futures.py")
s = p.read_text(encoding="utf-8", errors="replace").replace("\t","    ")

mark = "# --- KC3_TIME_SYNC ---"
if mark not in s:
    raise SystemExit("KC3_TIME_SYNC marker not found; refusing to patch blindly.")

m_start = s.find(mark)
m_priv = s.find("def private_req(", m_start)
if m_priv == -1:
    raise SystemExit("Could not find def private_req( after KC3_TIME_SYNC marker.")

# Keep everything before marker + replace block + keep from def private_req onward
prefix = s[:m_start]
suffix = s[m_priv:]

block = """
# --- KC3_TIME_SYNC ---
_KC3_TIME_OFFSET_MS = None
_KC3_TIME_OFFSET_TS = 0.0

def _kc3_now_ms() -> int:
    import time as _t
    return int(_t.time() * 1000)

def _kc3_refresh_time_offset_ms(force: bool = False) -> int:
    global _KC3_TIME_OFFSET_MS, _KC3_TIME_OFFSET_TS
    import time as _t
    # refresh at most every 60s unless forced
    if (not force) and (_KC3_TIME_OFFSET_MS is not None) and (_t.time() - _KC3_TIME_OFFSET_TS < 60):
        return int(_KC3_TIME_OFFSET_MS)
    try:
        d = requests.get(BASE_URL + "/fapi/v1/time", timeout=5).json()
        server = int(d.get("serverTime"))
        local = _kc3_now_ms()
        _KC3_TIME_OFFSET_MS = server - local
        _KC3_TIME_OFFSET_TS = _t.time()
        return int(_KC3_TIME_OFFSET_MS)
    except Exception:
        if _KC3_TIME_OFFSET_MS is None:
            _KC3_TIME_OFFSET_MS = 0
        return int(_KC3_TIME_OFFSET_MS)

def _kc3_signed_timestamp_ms() -> int:
    off = _kc3_refresh_time_offset_ms(force=False)
    return _kc3_now_ms() + int(off)

def _kc3_is_timestamp_error(msg: str) -> bool:
    return ('"code":-1021' in msg) or ("'code': -1021" in msg) or ("Timestamp for this request is outside of the recvWindow" in msg)
"""

new_s = prefix + block + "\n" + suffix
p.write_text(new_s, encoding="utf-8")
print("OK: KC3_TIME_SYNC block replaced cleanly.")
PY

  echo "=== 2) Compile check ==="
  python3 -m py_compile kc3_execute_futures.py || exit 1
  echo "OK: kc3_execute_futures.py compiles"

  echo "=== DONE ==="
) 2>&1 | tee "$LOG"
