#!/usr/bin/env bash
# Stream compose service logs to ./logs/*.log (run from deploy/docker after `docker compose up -d`).
set -euo pipefail
COMPOSE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$COMPOSE_DIR"
mkdir -p "$COMPOSE_DIR/logs"
LOG="$COMPOSE_DIR/logs/stack-$(date +%Y%m%d-%H%M%S).log"
echo "Appending to $LOG (Ctrl+C to stop)"
docker compose logs -f --no-color temporal temporal-ui postgresql nexcraft-worker 2>&1 | tee -a "$LOG"
