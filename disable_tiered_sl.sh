#!/usr/bin/env bash
cd /root/bbotpat_live || exit 1
LOG="/root/bbotpat_live/disable_tiered_sl_$(date +%Y%m%d_%H%M%S).log"
echo "Logging to: $LOG"
echo "If anything scrolls: tail -n 200 $LOG"
(
  set -u
  umask 022

  echo "=== 0) Backup .env ==="
  cp -a .env ".env.bak.$(date +%Y%m%d_%H%M%S)" || { echo "Backup .env failed"; exit 1; }

  echo "=== 1) Disable SL ladder + ensure SL_PCT=0 ==="
  # remove existing ladder lines and SL_PCT line, then append clean settings
  grep -vE '^(KC3_SL_LADDER_LEVELS=|KC3_SL_LADDER_FRACTION=|KC3_SL_PCT=)' .env > .env.tmp 2>/dev/null || true
  mv .env.tmp .env

  {
    echo "KC3_SL_PCT=0"
    echo "KC3_SL_LADDER_LEVELS="
    echo "KC3_SL_LADDER_FRACTION=0"
  } >> .env

  echo "=== 2) Show resulting SL settings ==="
  egrep '^KC3_SL_' .env || true

  echo "=== 3) Restart executor only (robust) with new env ==="
  pkill -f kc3_execute_futures_robust.py 2>/dev/null || true
  sleep 1
  set -a; source .env; set +a
  nohup python3 kc3_execute_futures_robust.py > kc3_exec.log 2>&1 &

  sleep 2
  echo "=== 4) Confirm running + confirm env inside process ==="
  pgrep -af kc3_execute_futures_robust.py || true
  PID=$(pgrep -f kc3_execute_futures_robust.py | head -n 1)
  echo "PID=$PID"
  if [ -n "$PID" ]; then
    tr '\0' '\n' < /proc/$PID/environ | egrep '^KC3_SL_' | sort || true
  fi

  echo "=== DONE ==="
) 2>&1 | tee -a "$LOG"
