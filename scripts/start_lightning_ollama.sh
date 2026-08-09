#!/usr/bin/env bash
# Restart Ollama server on Lightning Studio (models already pulled).
set -euo pipefail
echo "Starting Ollama on 0.0.0.0:11434..."
OLLAMA_HOST=0.0.0.0 ollama serve
