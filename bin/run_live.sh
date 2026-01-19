#!/usr/bin/env bash
set -euo pipefail
cd /root/bbotpat_live
exec /root/bbotpat_live/.venv/bin/python -u /root/bbotpat_live/hiveai_live_collector.py
