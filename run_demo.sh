#!/usr/bin/env bash
# run_demo.sh — Start the snow-tf-platform demo backend on port 8001
#
# Usage:
#   cd /home/snatesan/projects/snow-tf-platform
#   ./run_demo.sh
#
# Then in a separate terminal:
#   cd /home/snatesan/WorkbenchIQ-new/frontend
#   npm run dev
#
# Open http://localhost:3000/snow

set -euo pipefail

echo "Starting snow-tf-platform demo backend on port 8001 ..."
python3 -m uvicorn demo_server:app --port 8001 --reload --log-level info
