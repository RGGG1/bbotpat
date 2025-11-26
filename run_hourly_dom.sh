#!/usr/bin/env bash
set -euo pipefail

cd /root/bbotpat

# Activate virtualenv for hourly DOM pipeline
if [ -d ".venv" ]; then
  # shellcheck disable=SC1091
  source .venv/bin/activate
fi


echo "[run_hourly_dom] Starting at $(date -Is)"

# 1) Update prices, dominance, bands, HMI
python3 export_prices.py
python3 update_dominance.py
python3 update_dom_mc_history.py
python3 compute_fg2_index.py
python3 export_hmi_json.py

# 2) Run DOM hourly algorithm (decision + state + signals)
python3 hourly_dom_algo.py

# 3) Build trade plan (no actual execution yet)
python3 dom_trade_plan.py

# 4) Execute trades (STUB: logs what it would do)
python3 execute_dom_trade.py

# 5) Send Telegram update
python3 send_dom_hourly_telegram.py

echo "[run_hourly_dom] Done at $(date -Is)"
