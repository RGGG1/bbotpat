#!/usr/bin/env bash
set -u
cd /root/bbotpat_live || exit 1

LOG="/root/bbotpat_live/patch_tp_observable_$(date +%Y%m%d_%H%M%S).log"
echo "Logging to: $LOG"
echo "If anything scrolls: tail -n 200 $LOG"

(
  umask 022

  echo "=== 0) Backup robust ==="
  cp -a kc3_execute_futures_robust.py "kc3_execute_futures_robust.py.bak.$(date +%Y%m%d_%H%M%S)" || exit 1

  echo "=== 1) Patch: add rate-limited TP_CHECK logging BEFORE TP hit ==="
  python3 - <<'PY'
from pathlib import Path
import re

p = Path("kc3_execute_futures_robust.py")
s = p.read_text(encoding="utf-8", errors="replace").replace("\t","    ")

# Ensure we have a tiny rate-limit dict near top-level (idempotent)
if "_TP_LAST_LOG" not in s:
    # place after imports (best effort)
    m = re.search(r'(?m)^(import .+\n)+', s)
    insert_at = m.end() if m else 0
    s = s[:insert_at] + "\n_TP_LAST_LOG = {}  # sym -> epoch seconds (rate-limit TP_CHECK)\n" + s[insert_at:]

# Find the TP/SL block where tp is computed (we saw this in your snippet)
# We will inject logging right after tp is finalized (after per-trade override) and before TP hit condition.
needle = "if tp > 0 and (roi >= tp):"
pos = s.find(needle)
if pos == -1:
    raise SystemExit("Could not find TP hit condition; refusing to patch blindly.")

# Find the line start of that needle
line_start = s.rfind("\n", 0, pos) + 1
indent = re.match(r'^(\s*)', s[line_start:]).group(1)

inject = f"""{indent}# --- KC3_TP_CHECK_OBSERVABILITY ---
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

# Inject only if not already injected
if "KC3_TP_CHECK_OBSERVABILITY" not in s:
    s = s[:line_start] + inject + s[line_start:]

p.write_text(s, encoding="utf-8")
print("OK: TP_CHECK observability injected (30s rate limit).")
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

  echo "=== 4) Confirm TP_CHECK strings exist in file ==="
  grep -n "KC3_TP_CHECK_OBSERVABILITY" -n kc3_execute_futures_robust.py || true

) 2>&1 | tee "$LOG"
