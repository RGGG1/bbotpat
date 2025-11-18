#!/usr/bin/env bash
set -e

cd /root/bbotpat
source .env

# Make sure we have latest code
git pull

# Compute HMI and export JSON
python3 compute_fg2_index.py
python3 export_hmi_json.py

# If export_hmi_json.py only writes hmi_latest.json at root,
# ensure docs copy exists for GitHub Pages:
if [ -f hmi_latest.json ]; then
  cp hmi_latest.json docs/hmi_latest.json
fi

# Commit and push any changes
git add -A
git commit -m "Update HMI (auto)" || true
git push
