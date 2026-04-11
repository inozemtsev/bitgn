#!/bin/bash
set -e

echo "=== Run 1: gpt-5.4 ==="
WORKERS=6 MODEL_ID=gpt-5.4 RUN_NAME=codex-on-rails uv run python main.py 2>&1 | tee run_improved_all_$(date +%Y%m%d_%H%M).log

echo ""
echo "=== Run 2: gpt-5.3-codex ==="
WORKERS=6 MODEL_ID=gpt-5.3-codex RUN_NAME=codex-on-rails uv run python main.py 2>&1 | tee run_improved_all_$(date +%Y%m%d_%H%M).log
