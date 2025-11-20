#!/usr/bin/env bash
set -euo pipefail

LOG_FILE="/root/bbotpat/log_hmi.log"

{
  echo "[$(date -u)] --- run_hmi.sh START ---"

  cd /root/bbotpat

  # Make sure scripts are executable
  chmod +x run_hmi.sh run_dom.sh || true

  # Activate venv and env vars
  if [ -f ".venv/bin/activate" ]; then
    # shellcheck source=/dev/null
    source .venv/bin/activate
  else
    echo "[$(date -u)] ERROR: .venv/bin/activate not found"
  fi

  if [ -f ".env" ]; then
    # shellcheck source=/dev/null
    source .env
  else
    echo "[$(date -u)] WARNING: .env not found"
  fi

  echo "[$(date -u)] Running git pull…"
  git pull --ff-only || echo "[$(date -u)] WARNING: git pull failed (non-fatal)"

  echo "[$(date -u)] Running compute_fg2_index.py…"
  python3 compute_fg2_index.py

  # compute_fg2_index.py already writes hmi_latest.json and docs/hmi_latest.json
  if [ -f "hmi_latest.json" ]; then
    echo "[$(date -u)] HMI file present: hmi_latest.json"
  else
    echo "[$(date -u)] ERROR: hmi_latest.json not found after compute_fg2_index.py"
  fi

  echo "[$(date -u)] Staging HMI artefacts…"
  git add \
    hmi_latest.json \
    docs/hmi_latest.json \
    data/hmi_oi_history.csv \
    output/fg2_daily.csv 2>/dev/null || true

  echo "[$(date -u)] Committing HMI changes if any…"
  git commit -m "Update HMI (auto)" || echo "[$(date -u)] No HMI changes to commit"

  echo "[$(date -u)] Pushing to origin…"
  git push || echo "[$(date -u)] WARNING: git push failed"

  echo "[$(date -u)] --- run_hmi.sh END ---"
} >> "$LOG_FILE" 2>&1
