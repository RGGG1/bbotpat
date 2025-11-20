#!/usr/bin/env bash
set -e

LOG=/root/hmi.log

{
  echo "[$(date -u '+%Y-%m-%d %H:%M:%S UTC')] --- run_hmi.sh START ---"

  cd /root/bbotpat

  # Load secrets
  if [ -f .env ]; then
    # shellcheck disable=SC1091
    source .env
  fi

  # Activate Python virtualenv
  # shellcheck disable=SC1091
  source .venv/bin/activate

  # Compute HMI and update JSONs
  python3 compute_fg2_index.py

  echo "[$(date -u '+%Y-%m-%d %H:%M:%S UTC')] --- run_hmi.sh END (OK) ---"
} >> "$LOG" 2>&1
