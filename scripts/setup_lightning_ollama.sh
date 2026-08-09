#!/usr/bin/env bash
# APEX TRADER — Lightning AI Studio setup for Ollama GPU inference
# Run this inside your Lightning Studio terminal to set up all local models.
# After running, note your Studio's public URL and set LIGHTNING_OLLAMA_URL in .env.

set -euo pipefail

echo "=== APEX TRADER — Lightning Ollama Setup ==="

# 1. Install Ollama
echo "[1/4] Installing Ollama..."
curl -fsSL https://ollama.ai/install.sh | sh

# 2. Start Ollama in background, bound to all interfaces
echo "[2/4] Starting Ollama server (0.0.0.0:11434)..."
OLLAMA_HOST=0.0.0.0 nohup ollama serve > /tmp/ollama.log 2>&1 &
sleep 5  # wait for server to start

# 3. Pull all 5 models used by APEX
echo "[3/4] Pulling models (this takes a few minutes on first run)..."
ollama pull llama3.1:8b
ollama pull qwen2.5:14b
ollama pull deepseek-r1:7b
ollama pull mistral:7b
ollama pull gemma2:9b

# 4. Verify
echo "[4/4] Verifying..."
ollama list
echo ""
echo "=== Setup complete! ==="
echo ""
echo "Your Ollama server is running on port 11434."
echo ""
echo "To expose it publicly in Lightning Studio:"
echo "  1. Click 'Open Port' in Lightning Studio UI"
echo "  2. Enter port 11434"
echo "  3. Copy the public URL (e.g. https://xxxxx.lightning.ai)"
echo "  4. Set in your APEX .env:"
echo "     LIGHTNING_OLLAMA_URL=https://xxxxx.lightning.ai"
echo ""
echo "To keep Ollama running after this script exits:"
echo "  OLLAMA_HOST=0.0.0.0 ollama serve"
