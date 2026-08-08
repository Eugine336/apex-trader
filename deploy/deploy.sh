#!/usr/bin/env bash
set -euo pipefail

# Quick deploy: pull latest, rebuild frontend, restart API.
APEX_DIR="/opt/apex-trader"

echo "=== Pulling latest code ==="
cd "$APEX_DIR"
git pull

echo "=== Updating Python deps ==="
./venv/bin/pip install -r requirements.txt --quiet

echo "=== Rebuilding frontend ==="
cd "$APEX_DIR/frontend"
npm install --silent
npm run build

echo "=== Restarting API ==="
sudo systemctl restart apex-api

echo "=== Done ==="
systemctl status apex-api --no-pager -l
