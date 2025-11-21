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

  # Rebalance real Binance spot using USDC hub + BTC shortcut
  python3 execute_trades.py

  echo "[$(date -u '+%Y-%m-%d %H:%M:%S UTC')] --- run_dom.sh END (OK) ---"
} >> "$LOG" 2>&1
