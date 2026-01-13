#!/usr/bin/env bash
set -u
cd /root/bbotpat_live || exit 1

LOG="/root/bbotpat_live/fix_robust_restore_and_start_$(date +%Y%m%d_%H%M%S).log"
echo "Logging to: $LOG"
echo "If anything scrolls: tail -n 200 $LOG"

(
  umask 022

  echo "=== 0) Stop robust if running ==="
  pkill -f kc3_execute_futures_robust.py 2>/dev/null || true
  sleep 1
  pgrep -af kc3_execute_futures_robust.py || echo "robust stopped"

  echo "=== 1) Backup current robust (even if broken) ==="
  cp -a kc3_execute_futures_robust.py "kc3_execute_futures_robust.py.bak.BROKEN_$(date +%Y%m%d_%H%M%S)" || true

  echo "=== 2) Restore newest robust backup that COMPILES ==="
  python3 - <<'PY'
import glob, subprocess, sys, shutil
cands = sorted(glob.glob("kc3_execute_futures_robust.py.bak.*"), reverse=True)
if not cands:
    print("NO BACKUPS FOUND: kc3_execute_futures_robust.py.bak.*")
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

shutil.copy2(ok, "kc3_execute_futures_robust.py")
print("RESTORED_FROM:", ok)
PY

  echo "=== 3) Compile check robust ==="
  python3 -m py_compile kc3_execute_futures_robust.py
  echo "OK: robust compiles"

  echo "=== 4) Start robust ==="
  set -a; source .env; set +a
  nohup python3 kc3_execute_futures_robust.py >> kc3_exec.log 2>&1 &
  sleep 2
  pgrep -af kc3_execute_futures_robust.py || echo "ROBUST NOT RUNNING"

  echo "=== 5) Tail log ==="
  tail -n 40 kc3_exec.log
) 2>&1 | tee "$LOG"
