#!/usr/bin/env bash
set -e

DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$DIR/frontend"

if [ ! -d "node_modules" ]; then
    echo "[*] Installing frontend dependencies..."
    npm install
fi

echo "[*] Building React frontend..."
VITE_API_URL="" npm run build

echo "[✓] Frontend built at $DIR/frontend/build/"
echo "[✓] Restart the bot — dashboard will be served at http://0.0.0.0:8000"
