#!/usr/bin/env bash
set -euo pipefail
cd /root/bbotpat_live || exit 1

echo "=== stop robust ==="
pkill -f kc3_execute_futures_robust.py 2>/dev/null || true
sleep 1

echo "=== patch shim into kc3_execute_futures.py (append-only) ==="
python3 - <<'PY'
from pathlib import Path
p = Path("kc3_execute_futures.py")
s = p.read_text(errors="replace")

# Append-only shim: do NOT modify existing code.
# Provide _kc3_refresh_time_offset() expected by some code paths.
if "def _kc3_refresh_time_offset(" not in s:
    shim = r'''
# --- KC3_COMPAT_SHIM_REFRESH_TIME_OFFSET ---
def _kc3_refresh_time_offset(force: bool = False) -> int:
    """
    Compatibility shim.
    Some code calls _kc3_refresh_time_offset(), other versions define _kc3_refresh_time_offset_ms().
    Return offset in ms.
    """
    try:
        fn = globals().get("_kc3_refresh_time_offset_ms")
        if callable(fn):
            return int(fn(force))
    except Exception:
        pass
    # If we cannot compute an offset, safest fallback is 0 (system clock is NTP-synced).
    return 0
# --- END KC3_COMPAT_SHIM_REFRESH_TIME_OFFSET ---
'''
    s = s + "\n" + shim
    p.write_text(s)
    print("OK: shim appended (_kc3_refresh_time_offset)")
else:
    print("OK: _kc3_refresh_time_offset already exists (no change)")
PY

echo "=== compile check ==="
python3 -m py_compile kc3_execute_futures.py
python3 -m py_compile kc3_execute_futures_robust.py

echo "=== signed endpoint smoke test (must succeed) ==="
python3 - <<'PY'
import kc3_execute_futures as b
print("has private_req:", hasattr(b,"private_req"))
print("has get_position:", hasattr(b,"get_position"))
print("has _kc3_refresh_time_offset:", hasattr(b,"_kc3_refresh_time_offset"))
d = b.private_req("GET", "/fapi/v2/positionRisk", {})
print("OK positionRisk rows:", len(d))
PY

echo "=== restart robust ==="
PYTHONUNBUFFERED=1 nohup python3 kc3_execute_futures_robust.py >> kc3_exec.log 2>&1 &
sleep 2
echo "ROBUST_PID=$(pgrep -f kc3_execute_futures_robust.py | head -n 1)"
tail -n 30 kc3_exec.log
