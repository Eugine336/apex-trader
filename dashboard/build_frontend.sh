#!/usr/bin/env bash
set -e

DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$DIR/frontend"

echo "[*] Working directory: $(pwd)"

if [ ! -d "node_modules" ]; then
    echo "[*] Installing frontend dependencies..."
    npm install
fi

echo "[*] Cleaning previous build..."
rm -rf build

echo "[*] Building React frontend..."
npm run build

echo ""
echo "[✓] Build complete — $(ls build/assets/*.js 2>/dev/null | wc -l) JS files in build/assets/"
echo "[✓] Restart the bot — dashboard will be served at http://0.0.0.0:8000"
