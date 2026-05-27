#!/usr/bin/env bash
# Usage: ontology-reindex-worker {start|stop|status|logs|restart} [-- extra reindex args]
#
# Runs the ontology-store REINDEX (indexing) worker:
#   .venv/bin/ontology-store reindex run-forever --env <slug>
# It drains the reindex_queue in the ontology-store Postgres ($ONTOLOGY_STORE_URL)
# and (re)builds the hier_t* / event Qdrant collections via OpenAI embeddings.
# Unlike the FedSQL/pipeline workers it does NOT use Temporal — it polls Postgres.
#
# The ontology-store CLI reads its config from the process environment (it does
# NOT load a .env), so this script sources nexcraft-jobs/.env first. It needs:
#   ONTOLOGY_STORE_URL, QDRANT_HOST (or QDRANT_URL), OPENAI_API_KEY (or EMBEDDING_API_KEY)
# Override the env file with ONTOLOGY_DOTENV=/path/to/.env; the env slug for the
# hier_t* collection names defaults to $ONTOLOGY_ENV (else "prod").
set -euo pipefail

NEXCRAFT=/Users/sameerm/ComplianceSpark/byziplatform/unstructured/nexcraft
JOBS_ENV="$NEXCRAFT/packages/nexcraft-jobs/.env"
DOTENV="${ONTOLOGY_DOTENV:-$JOBS_ENV}"
PID=/tmp/ontology-reindex-worker.pid
LOG=/tmp/ontology-reindex-worker.log

# Load KEY=VALUE lines from the .env WITHOUT shell expansion, so DSNs/passwords
# that contain $ or % survive intact (e.g. ONTOLOGY_STORE_URL).
load_dotenv() {
  local f="$1" line key val
  [[ -f "$f" ]] || return 0
  while IFS= read -r line || [[ -n "$line" ]]; do
    [[ "$line" =~ ^[[:space:]]*# ]] && continue
    [[ "$line" =~ ^[[:space:]]*$ ]] && continue
    key="${line%%=*}"; val="${line#*=}"
    key="${key#"${key%%[![:space:]]*}"}"; key="${key%"${key##*[![:space:]]}"}"
    [[ "$key" =~ ^[A-Za-z_][A-Za-z0-9_]*$ ]] || continue
    # strip one layer of surrounding quotes if present (bash 3.2-safe; no negative slices)
    if [[ ${#val} -ge 2 && "$val" == \"*\" ]]; then val="${val#\"}"; val="${val%\"}"; fi
    if [[ ${#val} -ge 2 && "$val" == \'*\' ]]; then val="${val#\'}"; val="${val%\'}"; fi
    export "$key=$val"
  done < "$f"
}

case "${1:-}" in
  start)
    if [[ -f $PID ]] && ps -p "$(cat "$PID")" > /dev/null 2>&1; then
      echo "Already running — PID $(cat "$PID")"; exit 0
    fi
    if [[ -n "$DOTENV" && ! -f "$DOTENV" ]]; then
      echo "WARN: ONTOLOGY_DOTENV=$DOTENV not found (need ONTOLOGY_STORE_URL/QDRANT_HOST/OPENAI_API_KEY)"
    fi
    cd "$NEXCRAFT"
    load_dotenv "$DOTENV"
    ENV_SLUG="${ONTOLOGY_ENV:-prod}"
    nohup .venv/bin/ontology-store reindex run-forever --env "$ENV_SLUG" "${@:2}" \
      > "$LOG" 2>&1 &
    echo $! > "$PID"
    sleep 1
    if ps -p "$(cat "$PID")" > /dev/null 2>&1; then
      echo "Started — PID $(cat "$PID"), env=$ENV_SLUG, log: $LOG"
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
    echo "Usage: $0 {start|stop|status|logs|restart} [-- extra reindex args]"; exit 1
    ;;
esac
