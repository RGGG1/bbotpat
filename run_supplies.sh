#!/usr/bin/env bash
set -e

cd /root/bbotpat

# Load secrets
source .env

# Activate Python virtualenv
source .venv/bin/activate

git pull

python update_supplies.py

git add -A
git commit -m "Update supplies (auto)" || true
git push
#!/usr/bin/env bash
set -e

cd /root/bbotpat
source .env

git pull

python3 update_supplies.py

git add -A
git commit -m "Update supplies (auto)" || true
git push
