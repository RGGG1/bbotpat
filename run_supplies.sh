#!/usr/bin/env bash
set -e

cd /root/bbotpat
source .env

git pull

python3 update_supplies.py

git add -A
git commit -m "Update supplies (auto)" || true
git push
