#!/usr/bin/env bash
set -euo pipefail

LOG_FILE="/root/bbotpat/log_dom.log"

{
  echo "[$(date -u)] --- run_dom.sh START ---"

  cd /root/bbotpat

  # Ensure executable bits
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

  echo "[$(date -u)] Running send_fg_dom_signal_telegram.py…"
  python3 send_fg_dom_signal_telegram.py

  # Script already writes root + docs JSON files
  if [ -f "dom_bands_latest.json" ]; then
    echo "[$(date -u)] dom_bands_latest.json present"
  else
    echo "[$(date -u)] ERROR: dom_bands_latest.json missing"
  fi

  if [ -f "prices_latest.json" ]; then
    echo "[$(date -u)] prices_latest.json present"
  else
    echo "[$(date -u)] ERROR: prices_latest.json missing"
  fi

  if [ -f "portfolio_weights.json" ]; then
    echo "[$(date -u)] portfolio_weights.json present"
  else
    echo "[$(date -u)] ERROR: portfolio_weights.json missing"
  fi

  echo "[$(date -u)] Staging DOM/prices/portfolio artefacts…"
  git add \
    dom_bands_latest.json docs/dom_bands_latest.json \
    prices_latest.json docs/prices_latest.json \
    portfolio_weights.json docs/portfolio_weights.json 2>/dev/null || true

  echo "[$(date -u)] Committing DOM changes if any…"
  git commit -m "Update dominance/prices/portfolio (auto)" || echo "[$(date -u)] No DOM changes to commit"

  echo "[$(date -u)] Pushing to origin…"
  git push || echo "[$(date -u)] WARNING: git push failed"

  echo "[$(date -u)] --- run_dom.sh END ---"
} >> "$LOG_FILE" 2>&1
