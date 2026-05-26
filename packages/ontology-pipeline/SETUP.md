# Setup — ontology-pipeline local preview

Step-by-step walkthrough for getting from a fresh checkout to "I'm watching
my workflow execute in the Temporal UI." Each step has a verification line
so you know whether it worked before moving on.

Estimated time: **~15 minutes** if Temporal is already installed, ~30
minutes from a clean machine.

> **What you'll end up with:** the full ontology pipeline running against
> the bundled CSOD learning dataset, every artifact written to
> `output/preview/`, orchestrated by a local Temporal server, with
> per-table parallelism and per-activity retry visible in the Temporal UI.
> No live Postgres / Qdrant / OpenAI API key required.

---

## Prerequisites

| What | Version | Why |
|------|---------|-----|
| **Python** | ≥ 3.11 | `pyproject.toml` requires it |
| **uv** or **pip** | recent | install deps + the editable packages |
| **Temporal CLI** _or_ **Docker** | any | run a local Temporal server |
| **macOS / Linux** | — | tested on macOS; Linux should work identically. Windows untested. |

You DO NOT need Postgres, Qdrant, an OpenAI key, or a DeepSeek key for
the preview walkthrough. They become relevant only when you want to flip
LLM stages on (covered at the end).

Check your Python:

```bash
python3 --version
# Python 3.11+ — anything older won't work
```

---

## 1. Get the repo

The walkthrough assumes you already have the monorepo checked out at:

```
/Users/sameerm/ComplianceSpark/byziplatform/unstructured/nexcraft/
```

If your path differs, substitute `$NEXCRAFT` for your root path everywhere
below.

```bash
export NEXCRAFT=/Users/sameerm/ComplianceSpark/byziplatform/unstructured/nexcraft
cd "$NEXCRAFT"

# Workspace layout you should see:
ls packages/
# admin  nexcraft  nexcraft-driver  nexcraft-jobs
# ontology-foundry  ontology-pipeline  ontology-retrieval  ontology-store

# Verify the CSOD dataset is present:
ls data/indexingsamples/ | head -3
# causal_training_events.csv
# curriculum_structure_core.csv
# manifest.json
```

If `data/indexingsamples/` is missing, the preview will fail with
`FileNotFoundError` on `schema.sql` — the dataset is what the rest of the
walkthrough operates on.

---

## 2. Create a Python virtual environment

One venv shared across packages. The walkthrough below assumes a venv at
`$NEXCRAFT/.venv` (which is what the existing setup uses).

```bash
cd "$NEXCRAFT"
python3 -m venv .venv
source .venv/bin/activate

# Verify
python --version    # Python 3.11+
which python        # …/.venv/bin/python
```

---

## 3. Install the four ontology packages (editable)

```bash
cd "$NEXCRAFT"
# Install in dependency order. Each line is editable (`-e`) so code
# changes are picked up without reinstall.
pip install -e packages/ontology-foundry
pip install -e packages/ontology-store
pip install -e packages/ontology-pipeline[dev,llm]
```

Verify:

```bash
python -c "
import ontology_foundry, ontology_store, ontology_pipeline
print('foundry  :', ontology_foundry.__file__.replace('$NEXCRAFT/', ''))
print('store    :', ontology_store.__file__.replace('$NEXCRAFT/', ''))
print('pipeline :', ontology_pipeline.__file__.replace('$NEXCRAFT/', ''))
"
# All three should print paths under packages/.
```

---

## 4. Tier 0 — Run preview mode in-process (no Temporal yet)

This is the fastest way to confirm the install works. It runs the full
pipeline against the CSOD dataset in a single Python process and writes
every artifact under `output/preview/`.

**Pure-deterministic** — no LLM, no DB, no network. Wall time ~10s.

```bash
cd "$NEXCRAFT/packages/ontology-pipeline"
ontology-pipeline run --config configs/csod_local_preview.example.yaml
```

Expected output (last line of the log):

```
Pipeline run complete: seen=11 processed=11 unchanged=0 errored=0 llm_calls=0 wall=10.7s
```

Verify the artifact tree:

```bash
tree output/preview -L 3
```

You should see:

```
output/preview/
├── mdl/csod-local/public/
│   ├── users_core.json
│   ├── training_core.json
│   └── ... (11 files)
├── column_stats/csod-local/public/
│   ├── users_core.aggregates.json
│   └── ... (11 files)
└── postgres/
    ├── column_stat/   (606 JSON files — one per column of every table)
    └── table_stat/    (11 JSON files — one per table)
```

Inspect one column-stat row:

```bash
ls output/preview/postgres/column_stat | head -3
cat output/preview/postgres/column_stat/$(ls output/preview/postgres/column_stat | head -1)
```

You should see a JSON dict with `column_rk`, `n_rows`, `null_rate`,
`distinct_count`, `cardinality_tier`, and `top_frequencies`.

**If this works, your install is good and you can move to Temporal.**

If it doesn't, jump to [Troubleshooting](#troubleshooting) below.

---

## 5. Install Temporal

### Option A — Temporal CLI (preferred)

```bash
# macOS
brew install temporal

# Linux
curl -sSf https://temporal.download/cli.sh | sh

# Verify
temporal --version
```

### Option B — Docker

If you don't want the CLI, you can run Temporal in Docker:

```bash
docker pull temporalio/auto-setup:1.24
```

---

## 6. Start Temporal

In a fresh terminal — leave it running:

```bash
# Option A (Temporal CLI)
temporal server start-dev

# Option B (Docker)
docker run --rm -p 7233:7233 -p 8233:8233 temporalio/auto-setup:1.24
```

You should see logs ending with something like
`Server:    localhost:7233` and `UI:        http://localhost:8233`.

Verify in another terminal:

```bash
nc -z localhost 7233 && echo "temporal_running" || echo "temporal_not_running"
# Expected: temporal_running
```

Open the UI to confirm: <http://localhost:8233> (should show "0 Workflows").

---

## 7. Install the `[temporal]` extra

The Temporal SDK is an optional dependency. Install it now:

```bash
cd "$NEXCRAFT/packages/ontology-pipeline"
pip install -e ".[temporal]"
```

Also install nexcraft-jobs (the YAML-job CLI) if it's not already
installed:

```bash
pip install -e "$NEXCRAFT/packages/nexcraft-jobs"
```

Verify:

```bash
which ontology-pipeline-temporal-worker   # …/.venv/bin/ontology-pipeline-temporal-worker
which nexcraft-yaml-job                    # …/.venv/bin/nexcraft-yaml-job
python -c "import temporalio; print('temporalio', temporalio.__version__)"
# Expected: temporalio 1.6+

# Note: the package is named `nexcraft-jobs` but the installed CLI binary
# is `nexcraft-yaml-job` (singular). `which nexcraft-jobs` will NOT find
# anything — that's expected.
```

---

## 8. Start the ontology-pipeline worker

In a new terminal (keep Temporal running in the first one):

```bash
cd "$NEXCRAFT/packages/ontology-pipeline"
source "$NEXCRAFT/.venv/bin/activate"

export TEMPORAL_TARGET=localhost:7233
python -m ontology_pipeline.temporal.worker \
    --task-queue ontology-pipeline-default
```

You should see:

```
[INFO] Worker built: target=localhost:7233 namespace=default task_queue=ontology-pipeline-default
```

The worker is now listening on the `ontology-pipeline-default` task
queue. **Leave this terminal running.**

Verify from a third terminal:

```bash
# Temporal CLI: list registered task queues' workers
temporal task-queue describe \
    --task-queue ontology-pipeline-default \
    --task-queue-type workflow

# Expected: one Identity row with an active poller.
```

---

## 9. Submit the preview job via nexcraft-jobs

In your third terminal (still in the venv):

```bash
cd "$NEXCRAFT"
export TEMPORAL_TARGET=localhost:7233

nexcraft-yaml-job run packages/ontology-pipeline/configs/csod_local_preview_temporal.example.yaml
```

The CLI submits the workflow and awaits its result (the example sets
`wait_for_result: true`).

You should see the worker's terminal start streaming activity logs:

```
[INFO] introspect found 11 tables
[INFO] HierarchyStoreSink: column_stat aggregates for csod-local.public.curriculum_structure_core …
[INFO] HierarchyStoreSink: column_stat aggregates for csod-local.public.ou_core …
…
[INFO] workflow complete: seen=11 processed=11 unchanged=0 errored=0 llm_calls=0
```

The CLI returns once the workflow finishes (typically 15–30 seconds for
this preview run with `per_table_concurrency=4`).

---

## 10. Inspect what landed

### Temporal UI

Open <http://localhost:8233> → click into the workflow run.

You'll see a tree of activities:

- 1 × `ontology.introspect_source` (fan-in step at the start)
- 11 × `ontology.process_one_table` (parallel — capped by
  `per_table_concurrency: 4` in the YAML)
- 1 × `ontology.run_cross_asset_causal` (skipped — stage disabled)
- 1 × `ontology.run_relation_induction` (skipped — stage disabled)
- 1 × `ontology.run_causal_validation` (returns `skipped: 1` — preview
  sink can't reach a DB)

Each activity row shows input / output / wall time / retry count. The
final workflow result is a `WorkflowSummary` JSON dict (visible under
"Result" on the workflow page).

### Filesystem artifacts

```bash
cd "$NEXCRAFT/packages/ontology-pipeline"
tree output/preview -L 3
```

Same shape as Tier 0:

```
output/preview/
├── mdl/csod-local/public/                # 11 MDL v2 documents
├── column_stats/csod-local/public/       # aggregates + samples per table
└── postgres/
    ├── column_stat/                       # 606 rows
    └── table_stat/                        # 11 rows
```

Sanity-check one MDL:

```bash
python -c "
import json
mdl = json.load(open('output/preview/mdl/csod-local/public/users_core.json'))
m = mdl['models'][0]
print('asset_rk     :', m['rk'])
print('description  :', (m.get('description') or '(none)')[:80])
print('columns      :', len(m['columns']))
print('first column :', m['columns'][0]['name'])
print('  comment    :', (m['columns'][0]['properties'].get('description') or '')[:60])
"
```

Expected:

```
asset_rk     : postgres://csod-local.csod_learning/public/users_core
description  : (none)
columns      : ~30  (varies by table)
first column : _last_touched_dt_utc
  comment    : UTC date and time when the record has been created or most…
```

The fact that the column comment is present confirms the SQL parser is
preserving `COMMENT ON COLUMN` text end-to-end.

---

## You're done — but let me show you what's possible from here

### Re-running

After the first run, every table's content_hash is recorded. Subsequent
runs short-circuit unchanged tables:

```bash
nexcraft-yaml-job run packages/ontology-pipeline/configs/csod_local_preview_temporal.example.yaml
# Expected: seen=11 processed=0 unchanged=11 …
```

To force re-enrichment, edit the YAML:

```yaml
input:
  pipeline:
    re_enrich_unchanged: true
```

### Turning on LLM stages

The preview YAML has every LLM-driven stage disabled. To exercise one:

1. **Set an API key** in the worker terminal (where the LLM calls
   happen — not where you submit the job):

   ```bash
   # In the WORKER terminal (terminal #2), kill it (Ctrl-C) and re-start:
   export DEEPSEEK_API_KEY=sk-…
   python -m ontology_pipeline.temporal.worker --task-queue ontology-pipeline-default
   ```

2. **Edit the YAML**:

   ```yaml
   input:
     pipeline:
       enrich_data_protection: true        # PII classification per column
       infer_relationships: true            # inferred FK suggestions
       induce_relation_schema: true         # foundry.induce_schema TBox
   ```

3. **Re-submit** from terminal #3:

   ```bash
   nexcraft-yaml-job run packages/ontology-pipeline/configs/csod_local_preview_temporal.example.yaml
   ```

Each stage adds files under `output/preview/`:

- `output/preview/postgres/data_protection_hint/<asset>.json`
- `output/preview/postgres/causal_candidate/<key>.json`
- `output/preview/postgres/relation_type/<predicate>__<domain>__<range>.json`
- `output/preview/qdrant/causal_events/<event_id>.json`
- `output/preview/qdrant/relation_events/<event_id>.json`
- `output/preview/reindex_queue.jsonl` (one JSONL line per task)

Every event payload is built using the **exact same** narrative builders
the production ReindexWorker uses — what you see in `output/preview/` is
faithful to what Qdrant would receive.

### Running against your own data

Drop a `schema.sql` + one `<table>.csv` per table + an optional
`manifest.json` into any directory, then edit the YAML's `source.local.*`
paths:

```yaml
input:
  source:
    source_id: my-source
    org_id: my-org
    kind: local_files
    local:
      schema_sql: /absolute/path/to/schema.sql
      data_dir:   /absolute/path/to/csv_dir
      manifest:   /absolute/path/to/manifest.json   # optional
      catalog_name: my_catalog
```

The SQL parser handles `CREATE TABLE`, `COMMENT ON COLUMN`,
`COMMENT ON TABLE`, inline `PRIMARY KEY (…)` / `REFERENCES …`, and
standalone `ALTER TABLE … ADD CONSTRAINT … PRIMARY KEY` / `FOREIGN KEY`
statements. See `tests/test_local_preview.py::TestSqlParser` for the
exact shapes that work.

---

## Troubleshooting

| Symptom | Likely cause | Fix |
|---------|-------------|-----|
| `ModuleNotFoundError: No module named 'ontology_pipeline'` | venv not activated or packages not installed editable | `source "$NEXCRAFT/.venv/bin/activate"` then re-run the `pip install -e` lines from Step 3 |
| `FileNotFoundError: schema.sql not found at …/indexingsamples/schema.sql` | The CSOD dataset is missing or the path in the YAML is wrong | Verify with `ls "$NEXCRAFT/data/indexingsamples/schema.sql"`. The YAML uses a relative path; run from `packages/ontology-pipeline/`. |
| `ImportError: No module named 'pandas'` | `compute_column_stats=True` but pandas wasn't pulled in | `pip install pandas` (it's transitive via the foundry tabular extra) |
| `nc -z localhost 7233` returns 1 | Temporal isn't running | Re-run Step 6 in a fresh terminal and leave it open |
| Worker exits with `TypeError: Activity cannot have keyword-only arguments` | You're on an older checkout (pre-fix) | `git pull` then re-run `pip install -e ".[temporal]"` |
| `nexcraft-jobs: command not found` | The CLI binary is `nexcraft-yaml-job` (singular), not `nexcraft-jobs`. The PACKAGE is named `nexcraft-jobs`; the SCRIPT entry-point it installs is `nexcraft-yaml-job`. | Use `nexcraft-yaml-job run …` (or check `pip show nexcraft-jobs` — if it returns metadata, the package is installed; just run the right binary). If `pip show` returns nothing: `pip install -e "$NEXCRAFT/packages/nexcraft-jobs"`. |
| Submitting the job hangs and never returns | Worker isn't running, or it's on the wrong task queue | Check the worker's terminal: it should log `Worker built: … task_queue=ontology-pipeline-default`. The YAML and the worker must use the SAME `task_queue`. |
| `output/preview/` is empty after a Temporal run | The worker's CWD differs from where you're looking. The YAML's `base_dir: ./output/preview` resolves against the WORKER's CWD. | Either start the worker from `packages/ontology-pipeline/` (then `output/preview/` lives there) or set an absolute `base_dir` in the YAML. |
| `run_causal_validation` activity shows `skipped: 1` | Expected. Preview sink can't reach a DB; the validator post-pass short-circuits with `"filesystem sink — DB validator unavailable"`. To run the validator, switch `output.kind` to `tee` (which writes to BOTH filesystem and ontology-store) — that's a separate setup. | — |
| `Environment variable 'DEEPSEEK_API_KEY' referenced but not set` | An LLM stage is on but no key is set | Either set `DEEPSEEK_API_KEY` in the worker terminal, or set every `pipeline.*` LLM stage to `false` |
| Workflow runs but every per-table activity returns `outcome: error` | Almost always a missing dependency or bad source path. Check the worker's stderr — the first traceback identifies the issue. | — |

---

## Reference

- **Architecture & event-sourcing details:** `TESTING.md` (tier-by-tier reference)
- **Pipeline package README:** `packages/ontology-pipeline/README.md`
- **Example configs:**
  - In-process preview: `configs/csod_local_preview.example.yaml`
  - Temporal preview: `configs/csod_local_preview_temporal.example.yaml`
  - Full live mode: `configs/csod_temporal_job.example.yaml`
- **Test files for parser / sink / activity contracts:**
  - `tests/test_local_preview.py` — SQL parser + CSV loader + PreviewSink
  - `tests/test_temporal_workflow.py` — workflow input shapes + (gated) integration
  - `tests/test_column_stats.py` — foundry profiling shape
