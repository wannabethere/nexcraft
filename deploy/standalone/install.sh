#!/usr/bin/env bash
# Standalone dev install from repo root (Python venv + editable packages).
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$ROOT"

if ! command -v python3.11 &>/dev/null; then
  echo "python3.11 not found on PATH. Install Python 3.11+ and retry." >&2
  exit 1
fi

python3.11 -m venv .venv
# shellcheck source=/dev/null
source .venv/bin/activate
python -m pip install -U pip
pip install -e "./packages/nexcraft[postgres,dev]" -e "./packages/nexcraft-jobs[dev]"

echo
echo "Installed into ${ROOT}/.venv"
echo "  source ${ROOT}/.venv/bin/activate"
echo "Temporal (pick one): temporal server start-dev   OR   cd deploy/docker && docker compose up -d"
echo "Worker:  export TEMPORAL_HOST=localhost:7233 NEXCRAFT_STAGING_ROOT=/tmp/nexcraft-staging"
echo "         python examples/run_demo_worker.py"
echo "Submit:  python examples/03_temporal_submit_sketch.py"
