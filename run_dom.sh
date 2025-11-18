#!/usr/bin/env bash
set -e

cd /root/bbotpat
source .env

git pull

# Dominance + prices + portfolio (Binance-based script you have now)
python3 send_fg_dom_signal_telegram.py

git add -A
git commit -m "Update dominance/prices/portfolio (auto)" || true
git push
