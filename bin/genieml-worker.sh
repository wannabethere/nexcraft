#!/usr/bin/env bash
# Usage: genieml-worker {start|stop|status|logs|restart}
#
# Manages the GenieML FedSQL Temporal worker that runs the jobs the context
# preparer's default SQL agent submits (nexcraft_fedsql_query +
# nexcraft_dstools_pipeline) on task queue `nexcraft-jobs`.
#
# Config (POSTGRES_*, NEXCRAFT_DEFAULT_SOURCE_ID, OPENAI_API_KEY, …) is loaded by
# the worker from nexcraft-jobs/.env via NEXCRAFT_DOTENV_PATH. Override with
# GENIEML_DOTENV=/path/to/.env if needed.
set -euo pipefail

ROOT=/Users/sameerm/ComplianceSpark/byziplatform/unstructured
NEXCRAFT="$ROOT/nexcraft"
JOBS_ENV="$NEXCRAFT/packages/nexcraft-jobs/.env"
DOTENV="${GENIEML_DOTENV:-$JOBS_ENV}"
PID=/tmp/genieml-worker.pid
LOG=/tmp/genieml-worker.log

case "${1:-}" in
  start)
    if [[ -f $PID ]] && ps -p "$(cat "$PID")" > /dev/null 2>&1; then
      echo "Already running — PID $(cat "$PID")"; exit 0
    fi
    if [[ -n "$DOTENV" && ! -f "$DOTENV" ]]; then
      echo "WARN: GENIEML_DOTENV=$DOTENV not found"
    fi
    cd "$NEXCRAFT"
    nohup env \
      NEXCRAFT_DOTENV_PATH="$DOTENV" \
      TEMPORAL_TARGET="${TEMPORAL_TARGET:-localhost:7233}" \
      NEXCRAFT_TASK_QUEUE="${NEXCRAFT_TASK_QUEUE:-nexcraft-jobs}" \
      .venv/bin/python -m nexcraft_jobs.runtime.genieml_worker \
      > "$LOG" 2>&1 &
    echo $! > "$PID"
    sleep 1
    if ps -p "$(cat "$PID")" > /dev/null 2>&1; then
      echo "Started — PID $(cat "$PID"), log: $LOG"
    else
      echo "Failed to start; tail of log:"; tail -20 "$LOG"; exit 1
    fi
    ;;
  stop)
    if [[ -f $PID ]] && ps -p "$(cat "$PID")" > /dev/null 2>&1; then
      kill -TERM "$(cat "$PID")"; sleep 1
      ps -p "$(cat "$PID")" > /dev/null 2>&1 && kill -KILL "$(cat "$PID")" || true
      rm -f "$PID"; echo "Stopped"
    else
      echo "Not running"
    fi
    ;;
  status)
    if [[ -f $PID ]] && ps -p "$(cat "$PID")" > /dev/null 2>&1; then
      echo "Running — PID $(cat "$PID")"
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
