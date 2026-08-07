#!/usr/bin/env bash
# Run this from your apex-trader root directory BEFORE restarting the bot.
# Old .pyc files cause the bot to run stale code even after you replace .py files.
echo "Purging __pycache__..."
find . -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null
find . -name "*.pyc" -delete 2>/dev/null
echo "Done. Now restart the bot."
