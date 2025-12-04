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

  # Run dominance / prices / portfolio / TG signal
  python3 send_fg_dom_signal_telegram.py

  # Update DOM market-cap history for API (any combo X vs Y)
  python3 update_dom_mc_history.py

  # Rebalance real Binance spot using USDC hub + BTC shortcut
  python3 execute_trades.py

  # Auto-commit and push latest DOM / prices / portfolio / Knifecatcher data
  if ! git diff --quiet \
      docs/dom_bands_latest.json dom_bands_latest.json \
      docs/prices_latest.json prices_latest.json \
      docs/portfolio_weights.json portfolio_weights.json \
      docs/knifecatcher_latest.json knifecatcher_latest.json \
      data/portfolio_tracker.json 2>/dev/null; then

    git add \
      docs/dom_bands_latest.json dom_bands_latest.json \
      docs/prices_latest.json prices_latest.json \
      docs/portfolio_weights.json portfolio_weights.json \
      docs/knifecatcher_latest.json knifecatcher_latest.json \
      data/portfolio_tracker.json

    git commit -m "Update DOM, prices, portfolio & Knifecatcher (auto)" \
      || echo "[git] nothing to commit"

    git push || echo "[git] push failed"
  fi

  echo "[$(date -u '+%Y-%m-%d %H:%M:%S UTC')] --- run_dom.sh END (OK) ---"
} >> "$LOG" 2>&1

cp /root/bbotpat/knifecatcher_latest.json /root/bbotpat/docs/ 2>/dev/null || true
/bin/bash /root/bbotpat/sync_docs.sh
