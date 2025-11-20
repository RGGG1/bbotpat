#!/usr/bin/env bash
set -e

cd /root/bbotpat

# Load secrets
source .env

# Activate Python virtualenv
source .venv/bin/activate

# Run dominance / prices / portfolio script
python3 send_fg_dom_signal_telegram.py

# Commit and push any changes (JSONs, etc.)
git add -A
git commit -m "Update dominance/prices/portfolio (auto)" || true
git push origin main || true
