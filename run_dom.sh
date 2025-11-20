#!/usr/bin/env bash
set -e

LOG=/root/dom.log

{
  echo "[$(date -u '+%Y-%m-%d %H:%M:%S UTC')] --- run_dom.sh START ---"

  cd /root/bbotpat

  # Load secrets
  if [ -f .env ]; then
    # shellcheck disable=SC1091
    source .env
  fi

  # Activate Python virtualenv
  # shellcheck disable=SC1091
  source .venv/bin/activate

  # Run dominance / prices / portfolio script
  python3 send_fg_dom_signal_telegram.py

  echo "[$(date -u '+%Y-%m-%d %H:%M:%S UTC')] --- run_dom.sh END (OK) ---"
} >> "$LOG" 2>&1
