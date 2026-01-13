#!/usr/bin/env bash
set -u
cd /root/bbotpat_live || exit 1

LOG="/root/bbotpat_live/patch_tp_observable_v2_$(date +%Y%m%d_%H%M%S).log"
echo "Logging to: $LOG"
echo "If anything scrolls: tail -n 200 $LOG"

(
  umask 022

  echo "=== 0) Backup robust ==="
  cp -a kc3_execute_futures_robust.py "kc3_execute_futures_robust.py.bak.$(date +%Y%m%d_%H%M%S)" || exit 1

  echo "=== 1) Patch robust: inject TP_CHECK (rate-limited) right before TP hit print ==="
  python3 - <<'PY'
from pathlib import Path
import re

p = Path("kc3_execute_futures_robust.py")
s = p.read_text(encoding="utf-8", errors="replace").replace("\t","    ")

# ensure rate-limit dict exists
if "_TP_LAST_LOG" not in s:
    m = re.search(r'(?m)^(import .+\n)+', s)
    insert_at = m.end() if m else 0
    s = s[:insert_at] + "\n_TP_LAST_LOG = {}  # sym -> epoch seconds (rate-limit TP_CHECK)\n" + s[insert_at:]

# find the existing TP hit print line (you showed it exists)
hit_pat = r'(?m)^(?P<indent>\s*)print\(f"\[\{utc\(\)\}\] TP hit '
m = re.search(hit_pat, s)
if not m:
    raise SystemExit("Could not find TP hit print; refusing to patch blindly.")

indent = m.group("indent")

# if already injected, do nothing
if "KC3_TP_CHECK_OBSERVABILITY_V2" in s:
    print("OK: TP observability already present; no change.")
else:
    inject = f"""{indent}# --- KC3_TP_CHECK_OBSERVABILITY_V2 ---
{indent}try:
{indent}    import time as _t
{indent}    _now = _t.time()
{indent}    _last = _TP_LAST_LOG.get(sym, 0)
{indent}    if _now - _last >= 30:
{indent}        _TP_LAST_LOG[sym] = _now
{indent}        msg = f"[{{utc()}}] TP_CHECK {{sym}} roi={{roi:.4f}} tp={{tp:.4f}} tp_thr={{tp_thr:.4f}} mode={{tp_mode}} vol={{(tp_vol if tp_vol is not None else 'NA')}}"
{indent}        print(msg, flush=True)
{indent}        try:
{indent}            _kc3_filelog(msg)
{indent}        except Exception:
{indent}            pass
{indent}except Exception:
{indent}    pass

"""
    s = s[:m.start()] + inject + s[m.start():]
    p.write_text(s, encoding="utf-8")
    print("OK: injected TP_CHECK observability before TP hit print (30s rate limit).")
PY

  echo "=== 2) Compile robust ==="
  python3 -m py_compile kc3_execute_futures_robust.py
  echo "OK: robust compiles"

  echo "=== 3) Restart robust ==="
  pkill -f kc3_execute_futures_robust.py 2>/dev/null || true
  sleep 1
  set -a; source .env; set +a
  nohup python3 kc3_execute_futures_robust.py >> kc3_exec.log 2>&1 &
  sleep 2
  pgrep -af kc3_execute_futures_robust.py || echo "ROBUST NOT RUNNING"

  echo "=== 4) Confirm marker present ==="
  grep -n "KC3_TP_CHECK_OBSERVABILITY_V2" kc3_execute_futures_robust.py || true

) 2>&1 | tee "$LOG"
