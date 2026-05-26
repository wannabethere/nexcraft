#!/usr/bin/env bash
# Usage: ontology-worker {start|stop|status|logs|restart}
set -euo pipefail

NEXCRAFT=/Users/sameerm/ComplianceSpark/byziplatform/unstructured/nexcraft
PID=/tmp/ontology-worker.pid
LOG=/tmp/ontology-worker.log

case "${1:-}" in
  start)
    if [[ -f $PID ]] && ps -p $(cat $PID) > /dev/null 2>&1; then
      echo "Already running — PID $(cat $PID)"; exit 0
    fi
    cd "$NEXCRAFT"
    nohup env \
      TEMPORAL_TARGET=localhost:7233 \
      ${DEEPSEEK_API_KEY:+DEEPSEEK_API_KEY="$DEEPSEEK_API_KEY"} \
      .venv/bin/python -m ontology_pipeline.temporal.worker \
          --task-queue ontology-pipeline-default \
      > "$LOG" 2>&1 &
    echo $! > "$PID"
    sleep 1
    if ps -p $(cat $PID) > /dev/null 2>&1; then
      echo "Started — PID $(cat $PID), log: $LOG"
    else
      echo "Failed to start; tail of log:"; tail -20 "$LOG"; exit 1
    fi
    ;;
  stop)
    if [[ -f $PID ]] && ps -p $(cat $PID) > /dev/null 2>&1; then
      kill -TERM $(cat $PID); sleep 1
      ps -p $(cat $PID) > /dev/null 2>&1 && kill -KILL $(cat $PID) || true
      rm -f "$PID"; echo "Stopped"
    else
      echo "Not running"
    fi
    ;;
  status)
    if [[ -f $PID ]] && ps -p $(cat $PID) > /dev/null 2>&1; then
      echo "Running — PID $(cat $PID)"
    else
      echo "Not running"
    fi
    ;;
  logs)
    tail -f "$LOG"
    ;;
  restart)
    "$0" stop; "$0" start
    ;;
  *)
    echo "Usage: $0 {start|stop|status|logs|restart}"; exit 1
    ;;
esac
