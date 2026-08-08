#!/usr/bin/env bash
set -euo pipefail

# APEX Trader VPS Setup Script
# Run as root on a fresh Ubuntu 22.04+ VPS.
#
# Prerequisites:
#   - Domain apex-trader.live added to Cloudflare
#   - Cloudflare Tunnel credentials available

APEX_DIR="/opt/apex-trader"
APEX_DATA_DIR="/opt/apex-trader-data"
APEX_USER="apex"
DATA_REPO_URL="https://github.com/Eugine336/apex-trader-data.git"

echo "=== Creating apex user ==="
id -u "$APEX_USER" &>/dev/null || useradd --system --shell /bin/bash --home-dir "$APEX_DIR" "$APEX_USER"

echo "=== Installing system dependencies ==="
apt-get update -qq
apt-get install -y -qq python3 python3-venv python3-pip nodejs npm git

echo "=== Cloning source repository ==="
if [ ! -d "$APEX_DIR/.git" ]; then
    git clone https://github.com/Eugine336/apex-trader.git "$APEX_DIR"
else
    cd "$APEX_DIR" && git pull
fi

echo "=== Cloning dedicated data repository ==="
# Runtime state (DB snapshots, journals, learned state) lives in a SEPARATE
# git repo with its own remote. It MUST NOT be a plain directory inside the
# source checkout — otherwise git data operations (sync/compaction/clean-start)
# walk up into the source repo's .git and can destroy its history.
if [ ! -d "$APEX_DATA_DIR/.git" ]; then
    git clone "$DATA_REPO_URL" "$APEX_DATA_DIR"
else
    cd "$APEX_DATA_DIR" && git pull
fi

echo "=== Wiring the data junction ($APEX_DIR/data -> ../apex-trader-data) ==="
# Remove a pre-existing PLAIN data/ directory only if it is not already the
# junction and not its own git repo — never delete real data.
if [ -L "$APEX_DIR/data" ]; then
    echo "  data junction already present"
elif [ -e "$APEX_DIR/data/.git" ]; then
    echo "  WARNING: $APEX_DIR/data is its own git repo, leaving as-is"
elif [ -d "$APEX_DIR/data" ]; then
    if [ -z "$(ls -A "$APEX_DIR/data" 2>/dev/null)" ]; then
        rmdir "$APEX_DIR/data"
        ln -s ../apex-trader-data "$APEX_DIR/data"
        echo "  replaced empty plain data/ with the junction"
    else
        echo "  ERROR: $APEX_DIR/data is a non-empty plain directory."
        echo "         Move its contents into $APEX_DATA_DIR, remove it, then re-run."
        exit 1
    fi
else
    ln -s ../apex-trader-data "$APEX_DIR/data"
    echo "  created data junction"
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
chown -R "$APEX_USER:$APEX_USER" "$APEX_DATA_DIR"

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
