#!/usr/bin/env bash
set -e

DIR="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(cd "$DIR/.." && pwd)"

echo "╔══════════════════════════════════════════╗"
echo "║   APEX TRADER — Dashboard Launcher       ║"
echo "╚══════════════════════════════════════════╝"

# Install Python deps if needed
if ! python -c "import fastapi" 2>/dev/null; then
    echo "[*] Installing dashboard Python dependencies..."
    pip install -r "$DIR/requirements.txt"
fi

# Install frontend deps if needed
if [ ! -d "$DIR/frontend/node_modules" ]; then
    echo "[*] Installing frontend dependencies..."
    cd "$DIR/frontend"
    npm install
    cd "$DIR"
fi

# Start API server in background
echo "[*] Starting API server on http://localhost:8000"
cd "$ROOT"
uvicorn dashboard.api:app --host 0.0.0.0 --port 8000 --reload &
API_PID=$!

# Start React dev server
echo "[*] Starting React dashboard on http://localhost:3000"
cd "$DIR/frontend"
npm start &
REACT_PID=$!

echo ""
echo "Dashboard running:"
echo "  API:       http://localhost:8000/api/status"
echo "  Frontend:  http://localhost:3000"
echo ""
echo "Press Ctrl+C to stop both servers."

trap "kill $API_PID $REACT_PID 2>/dev/null; exit 0" INT TERM
wait
