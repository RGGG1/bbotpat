#!/usr/bin/env bash
set -e

cd /root/bbotpat

# Load secrets
source .env

# Activate Python virtualenv
source .venv/bin/activate

# Compute HMI and update JSONs
python3 compute_fg2_index.py

# Commit and push any changes
git add -A
git commit -m "Update HMI (auto)" || true
git push origin main || true
