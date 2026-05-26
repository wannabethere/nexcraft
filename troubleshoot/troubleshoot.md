Let me check what's in your `.env` so we make sure the right keys land on the worker:

Good — you have `DEEPSEEK_API_KEY`, `DEEPSEEK_BASE_URL`, `NEXCRAFT_DATA` (probably), `NEXCRAFT_OUT` (probably), and a bunch of others. Two practical options:

## Option A — load .env into the worker process (recommended)

The cleanest pattern: have the worker process itself source `.env` at startup. That way you don't depend on having sourced it in your shell, and a fresh terminal can stop / start the worker without re-sourcing.

```bash
# Stop the existing worker
kill -TERM $(cat /tmp/ontology-worker.pid) 2>/dev/null
sleep 1

# Start with .env auto-loaded via a bash subshell
cd /Users/sameerm/ComplianceSpark/byziplatform/unstructured/nexcraft

nohup bash -c '
  set -a                                          # auto-export every var assigned below
  source packages/.env                            # loads all KEY=val pairs
  set +a
  exec .venv/bin/python -m ontology_pipeline.temporal.worker \
      --task-queue ontology-pipeline-default
' > /tmp/ontology-worker.log 2>&1 &

echo $! > /tmp/ontology-worker.pid
sleep 2

# Verify
echo "--- worker boot log ---"
head -5 /tmp/ontology-worker.log
echo "--- env actually inherited (just key names) ---"
ps eww $(cat /tmp/ontology-worker.pid) | tr ' ' '\n' \
    | grep -E "^(DEEPSEEK|TEMPORAL|NEXCRAFT|ONTOLOGY)" | sed 's/=.*$/=***/'
```

The `set -a` / `set +a` trick automatically exports every variable assigned inside that block — so plain `KEY=value` lines in `.env` get exported to the child python process. `exec` replaces the bash subshell with the python worker so you don't get an extra parent process hanging around.

## Option B — rely on your already-sourced shell

If you already ran `source packages/.env` in your shell, child processes inherit your shell's env. Just run nohup without an `env` prefix:

```bash
cd /Users/sameerm/ComplianceSpark/byziplatform/unstructured/nexcraft
source packages/.env   # if you haven't already in this shell

# `env` ISN'T needed — nohup inherits all your shell's exported vars
nohup .venv/bin/python -m ontology_pipeline.temporal.worker \
    --task-queue ontology-pipeline-default \
  > /tmp/ontology-worker.log 2>&1 &

echo $! > /tmp/ontology-worker.pid
```

Downside: if you ever start a fresh terminal and forget to `source packages/.env` before launching the worker, the worker boots without the keys.

## Caveat — `.env` files with quotes / spaces

`source` in zsh handles `KEY=value` lines fine. It chokes on lines like `KEY=value with spaces` (without quotes). If your .env has any of those, switch to:

```bash
# Use Python's dotenv to load — more robust than `source`
nohup bash -c '
  export $(grep -v "^#" packages/.env | xargs -d "\n" -I {} echo {})
  exec .venv/bin/python -m ontology_pipeline.temporal.worker \
      --task-queue ontology-pipeline-default
' > /tmp/ontology-worker.log 2>&1 &
```

Or install `python-dotenv` and use that — but for your file (all simple `KEY=value`), `set -a; source; set +a` works.

## Verify the worker has the LLM key

```bash
# Should show DEEPSEEK_API_KEY=… in the process env
ps eww $(cat /tmp/ontology-worker.pid) | tr ' ' '\n' | grep "^DEEPSEEK_API_KEY=" | sed 's/=.*/=***/'
# Expected: DEEPSEEK_API_KEY=***
```

And the boot log shouldn't show the "no provider" warning:

```bash
grep -i "provider" /tmp/ontology-worker.log
# Expected: nothing (silent = provider built fine)
# If you see "no provider could be built" — the env var didn't land.
```

## Then submit

Since your `.env` also has `NEXCRAFT_DATA` / `NEXCRAFT_OUT` (or you can add them), the submitter terminal also needs those exported. Same `source` trick works:

```bash
# In your submitter terminal — fresh or existing
cd /Users/sameerm/ComplianceSpark/byziplatform/unstructured/nexcraft
set -a; source packages/.env; set +a

# Verify the two the YAML needs
echo "NEXCRAFT_DATA=$NEXCRAFT_DATA"
echo "NEXCRAFT_OUT=$NEXCRAFT_OUT"
# If either is empty, add them to packages/.env:
#   NEXCRAFT_DATA=/Users/sameerm/ComplianceSpark/byziplatform/unstructured/nexcraft/data
#   NEXCRAFT_OUT=/Users/sameerm/ComplianceSpark/byziplatform/unstructured/nexcraft/output

# Submit
nexcraft-yaml-job run \
    packages/ontology-pipeline/configs/csod_local_preview_temporal.example.yaml
```

Watch the worker log — you should now see real LLM activity:

```bash
tail -f /tmp/ontology-worker.log
# Lines like:
#   [INFO] RichDescriptionEnricher LLM call for postgres://csod-local.csod_learning/public/users_core
#   [INFO] HierarchyStoreSink: causal_candidate writes for csod-local.public.users_core — inserted=3 updated=0
#   [INFO] Relation induction: 17 edges → 4 predicates → 14 edge attachments
```

And after the run, the preview tree gets the new directories:

```bash
ls $NEXCRAFT_OUT/preview/
# annotations  causal_candidates  column_stats  data_protection_hints
# inferred_relationships  mdl  postgres  qdrant  relation_schema  reindex_queue.jsonl

# Counts — these are 0 in deterministic-only runs, > 0 once LLM stages fire:
ls $NEXCRAFT_OUT/preview/postgres/causal_candidate     2>/dev/null | wc -l
ls $NEXCRAFT_OUT/preview/postgres/data_protection_hint 2>/dev/null | wc -l
ls $NEXCRAFT_OUT/preview/postgres/relation_type        2>/dev/null | wc -l
ls $NEXCRAFT_OUT/preview/qdrant/causal_events          2>/dev/null | wc -l
ls $NEXCRAFT_OUT/preview/qdrant/relation_events        2>/dev/null | wc -l
ls $NEXCRAFT_OUT/preview/qdrant/protection_events      2>/dev/null | wc -l
wc -l $NEXCRAFT_OUT/preview/reindex_queue.jsonl
```

Total wall time with all 9 LLM stages on: probably **5–10 minutes** for 11 tables × ~7 LLM calls each at `per_table_concurrency=4`. The Temporal UI is the best way to watch — it shows each activity's input/output/duration in real time.