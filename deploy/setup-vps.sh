#!/usr/bin/env bash
set -euo pipefail

# APEX Trader VPS Setup Script
# Run as root on a fresh Ubuntu 22.04+ VPS.
#
# Prerequisites:
#   - Domain apex-trader.live added to Cloudflare
#   - Cloudflare Tunnel credentials available

APEX_DIR="/opt/apex-trader"
APEX_USER="apex"

echo "=== Creating apex user ==="
id -u "$APEX_USER" &>/dev/null || useradd --system --shell /bin/bash --home-dir "$APEX_DIR" "$APEX_USER"

echo "=== Installing system dependencies ==="
apt-get update -qq
apt-get install -y -qq python3 python3-venv python3-pip nodejs npm git

echo "=== Cloning repository ==="
if [ ! -d "$APEX_DIR/.git" ]; then
    git clone https://github.com/Eugine336/apex-trader.git "$APEX_DIR"
else
    cd "$APEX_DIR" && git pull
fi

echo "=== Setting up Python virtualenv ==="
cd "$APEX_DIR"
python3 -m venv venv
./venv/bin/pip install --upgrade pip
./venv/bin/pip install -r requirements.txt

echo "=== Building frontend ==="
cd "$APEX_DIR/frontend"
npm install
npm run build

echo "=== Setting permissions ==="
chown -R "$APEX_USER:$APEX_USER" "$APEX_DIR"

echo "=== Installing systemd services ==="
cp "$APEX_DIR/deploy/apex-api.service" /etc/systemd/system/
cp "$APEX_DIR/deploy/cloudflared.service" /etc/systemd/system/
systemctl daemon-reload
systemctl enable apex-api

echo ""
echo "=== Setup complete ==="
echo ""
echo "Next steps:"
echo "  1. Install cloudflared: https://developers.cloudflare.com/cloudflare-one/connections/connect-networks/downloads/"
echo "  2. Run: cloudflared tunnel login"
echo "  3. Run: cloudflared tunnel create apex-trader"
echo "  4. Update deploy/cloudflared-config.yml with your tunnel ID and credentials path"
echo "  5. Add DNS CNAME: apex-trader.live -> <tunnel-id>.cfargotunnel.com"
echo "     (or: cloudflared tunnel route dns apex-trader apex-trader.live)"
echo "  6. Start services:"
echo "     systemctl start apex-api"
echo "     systemctl start cloudflared"
echo "  7. Register at https://apex-trader.live (first user = admin)"
