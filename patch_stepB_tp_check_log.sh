#!/usr/bin/env bash
cd /root/bbotpat_live || exit 1
LOG="/root/bbotpat_live/patch_stepB_tp_check_log_$(date +%Y%m%d_%H%M%S).log"
echo "Logging to: $LOG"
echo "If anything scrolls: tail -n 200 $LOG"
(
  set -u
  umask 022

  echo "=== 0) Backup robust ==="
  cp -a kc3_execute_futures_robust.py "kc3_execute_futures_robust.py.bak.$(date +%Y%m%d_%H%M%S)" || exit 1

  echo "=== 1) Patch: add TP_CHECK prints (only if missing) ==="
  python3 - <<'PY'
from pathlib import Path
import re
p = Path("kc3_execute_futures_robust.py")
s = p.read_text(encoding="utf-8", errors="replace").replace("\t","    ")

# If TP_CHECK already exists, do nothing
if re.search(r"\bTP_CHECK\b", s):
    print("OK: TP_CHECK already present; no change.")
    raise SystemExit(0)

# We’ll add a TP_CHECK print right before TP hit evaluation.
# Find the existing TP hit print line and inject above it.
hit_pat = r'(?m)^\s*print\(f"\[\{utc\(\)\}\] TP hit '
m = re.search(hit_pat, s)
if not m:
    raise SystemExit("Could not find TP hit print line to anchor injection.")

# Insert TP_CHECK a few lines above TP hit, inside same block:
inject = (
    "                            # --- KC3_TP_CHECK_LOG ---\n"
    "                            try:\n"
    "                                print(f\"[{utc()}] TP_CHECK {sym} roi={roi:.4f} tp_thr={tp_thr:.4f} mode={tp_mode} vol={(tp_vol if tp_vol is not None else 'NA')}\", flush=True)\n"
    "                            except Exception:\n"
    "                                pass\n"
)

# Insert just before the TP hit print (same indentation block)
s = s[:m.start()] + inject + s[m.start():]

p.write_text(s, encoding="utf-8")
print("OK: injected TP_CHECK logging before TP hit evaluation.")
PY

  echo "=== 2) Compile check ==="
  python3 -m py_compile kc3_execute_futures_robust.py || exit 1
  echo "OK: kc3_execute_futures_robust.py compiles"

  echo "=== DONE STEP B ==="
) 2>&1 | tee "$LOG"
