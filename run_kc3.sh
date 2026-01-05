#!/usr/bin/env bash
set -euo pipefail
cd /root/bbotpat_live
set -a
source /root/bbotpat_live/.env
set +a
nohup python3 /root/bbotpat_live/kc3_execute_futures_robust.py > /root/bbotpat_live/kc3_exec.log 2>&1 &
nohup python3 /root/bbotpat_live/kc3_hmi_momentum_agent.py > /root/bbotpat_live/kc3_agent.log 2>&1 &
echo "KC3 started"
