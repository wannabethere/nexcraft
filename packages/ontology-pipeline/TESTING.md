# Testing ontology-pipeline

Five tiers, each with a different setup cost. Most users start at **Tier 0**
— offline preview mode with the CSOD CSVs — then move up to Tier 0.5
(same preview, but driven by Temporal) when they want orchestration +
retry semantics, and only graduate to higher tiers for live backend
validation.

| Tier | What it covers | What it needs | How to run |
|------|----------------|---------------|------------|
| **0. Local preview (in-process)** | Full pipeline against local CSV+SQL files. Every artifact that WOULD land in Postgres / Qdrant is written to `output/preview/` for inspection. No DB, no Qdrant, no Temporal, no network (LLM stages off by default). | Just Python + the CSOD dataset on disk. | See [Run preview mode against CSOD](#run-preview-mode-against-csod) |
| **0.5 Local preview (via Temporal)** | Same offline run, but submitted as a Temporal job: per-table parallel fan-out, per-activity retry, Temporal UI observability. Output still lands in `output/preview/`. No live Postgres/Qdrant. | Host Temporal server on `localhost:7233`. | See [Run preview mode via Temporal](#run-preview-mode-via-temporal) |
| **1. Unit** | Pure-Python tests: enrichers, profilers, NER pre-pass, validators, induction, sinks, workflow input shapes, SQL parser, CSV loader. | `pip install -e .[dev,llm]` | `pytest` |
| **2. DAO** (Postgres-gated) | ORM + DAO methods against a real Postgres (Postgres-specific types: ARRAY, JSONB, CHECK constraints, partial indexes). Skipped without a DB. | Postgres reachable + `ONTOLOGY_STORE_TEST_URL`. | `ONTOLOGY_STORE_TEST_URL=... pytest packages/ontology-store/tests` |
| **3. Temporal integration** | E2E workflow + activities against a host-provided Temporal server, with stubbed activity bodies. Per-table fan-out, post-pass ordering, summary shape. | Temporal reachable + `TEMPORAL_TARGET` + `RUN_TEMPORAL_TESTS=1`. | See [Tier 3 — Temporal integration test](#tier-3--temporal-integration-test) |

The "full E2E with real backends" walkthrough is in [Run against live
Postgres + Qdrant + Temporal](#run-against-live-postgres--qdrant--temporal)
— useful once you've validated the offline run and want to verify the
backend-bound paths.

---

## Tier 0 — Local preview mode (offline, recommended starting point)

This runs the **entire pipeline** against a SQL schema file + CSV data,
writing every artifact (MDL, annotations, column stats, the rows that
would go into Postgres, the events that would land in Qdrant, the reindex
queue tasks that would fire) to a structured tree under `output/preview/`.

**Zero backend setup**:
- No `docker run`. No `createdb`. No Temporal server.
- No `psycopg` connection. No `qdrant-client` connection.
- LLM stages default OFF so no API key is needed for the basic run.

### Run preview mode against CSOD

The CSOD learning dataset is at
`/Users/sameerm/ComplianceSpark/byziplatform/unstructured/nexcraft/data/indexingsamples/`:
- `schema.sql` — Postgres DDL for 11 tables with `COMMENT ON COLUMN` for every column.
- `manifest.json` — PK / FK hints per table.
- `*.csv` — actual data, one CSV per table.

#### 1. Install the package

```bash
cd packages/ontology-pipeline
pip install -e ".[dev,llm]"
```

`pandas` (transitively required by foundry profiling) lands as part of `dev`.

#### 2. Run the preview pipeline

Use the included example config:

```bash
cd packages/ontology-pipeline
ontology-pipeline run --config configs/csod_local_preview.example.yaml
```

You should see something like:

```
Pipeline run starting for source csod-local
Introspected 11 tables/views from csod-local
After filter: 11 table(s) (filter configured=False)
Pipeline run complete: seen=11 processed=11 unchanged=0 errored=0 llm_calls=0 wall=10.7s
```

#### 3. Inspect the output tree

```bash
tree output/preview -L 3
```

```
output/preview/
├── mdl/csod-local/public/
│   ├── users_core.json                          ← MDL v2 per table
│   ├── training_core.json
│   └── ... (11 files)
├── column_stats/csod-local/public/
│   ├── users_core.aggregates.json               ← scalar shape facts
│   ├── users_core.samples.json                  ← (PII-gated) top-k + sample rows
│   └── ...
├── postgres/                                    ← rows that WOULD persist to Postgres
│   ├── column_stat/<column_rk_safe>.json        (606 files for the CSOD dataset)
│   ├── table_stat/<table_rk_safe>.json          (11 files)
│   ├── causal_candidate/...                     (when causal stages enabled)
│   ├── data_protection_hint/...                 (when data_protection enabled)
│   ├── relation_type/...                        (when induce_relation_schema enabled)
│   └── lineage_edge/...
├── qdrant/                                      ← events that WOULD index to Qdrant
│   ├── causal_events/<event_id>.json
│   ├── relation_events/<event_id>.json
│   ├── protection_events/<event_id>.json
│   └── card_events/<event_id>.json
└── reindex_queue.jsonl                          ← one JSONL line per task the worker would dequeue
```

Each file is independently inspectable. Pick any column_stat row:

```bash
cat output/preview/postgres/column_stat/postgres*training_core_lo_interest_tracking_allowed.json
```

```json
{
  "column_rk": "postgres://csod-local.csod_learning/public/training_core/lo_interest_tracking_allowed",
  "table_rk":  "postgres://csod-local.csod_learning/public/training_core",
  "n_rows": 200,
  "null_rate": 0.0,
  "distinct_count": 2,
  "cardinality_tier": "low",
  "top_frequencies": [
    {"value": "f", "count": 194, "share": 0.97},
    {"value": "t", "count": 6,   "share": 0.03}
  ],
  "samples_persisted": true,
  "stats_are_approximate": false
}
```

#### 4. Validate the MDL

Each `mdl/<source>/<schema>/<table>.json` is a complete MDL v2 document
with all 11 CSOD column comments preserved verbatim:

```bash
cat output/preview/mdl/csod-local/public/users_core.json | jq '.models[0].columns[0]'
```

```json
{
  "name": "_last_touched_dt_utc",
  "type": "timestamp with time zone",
  "notNull": true,
  "rk": "postgres://csod-local.csod_learning/public/users_core/_last_touched_dt_utc",
  "properties": {
    "description": "UTC date and time when the record has been created or most recently updated…",
    "description_provenance": "extractor:sql_file"
  }
}
```

#### 5. Turn on LLM stages incrementally

The example config has every LLM-driven stage set to `false`. To exercise
one at a time, edit `configs/csod_local_preview.example.yaml`:

```yaml
llm:
  api_key_env: DEEPSEEK_API_KEY      # default — DeepSeek V3 via OpenAI-compatible client
  # Or switch to OPENAI_API_KEY + gpt-4o-mini

pipeline:
  compute_column_stats: true          # deterministic; on by default
  enrich_data_protection: true        # LLM — turn on to see PII classification
  infer_relationships: true           # LLM — see inferred FKs
  enrich_causal_dependencies: true    # LLM — see causal_node bindings
  induce_relation_schema: true        # LLM — see predicate canonicalization
  annotate: true                      # LLM — see concepts/key_areas binding
  concepts_source: ner_then_llm       # foundry NER pre-pass + LLM
```

Then set the API key:

```bash
export DEEPSEEK_API_KEY=...    # or OPENAI_API_KEY if you switched the YAML
ontology-pipeline run --config configs/csod_local_preview.example.yaml
```

Each new stage adds to the output tree:
- `output/preview/postgres/causal_candidate/...` — proposed causal edges
- `output/preview/qdrant/causal_events/...` — the events those candidates would emit
- `output/preview/relation_schema/...` — the induced predicate TBox

---

## Tier 0.5 — Local preview via Temporal (recommended for E2E validation)

Same offline run as Tier 0, but submitted as a Temporal workflow. You get
everything Tier 0 produces (`output/preview/` artifacts), plus:

- **Per-table parallelism**: `process_one_table` activities fan out
  concurrently up to `per_table_concurrency` (default 4).
- **Per-activity retry**: a transient failure on one table doesn't kill
  the run. Each activity has its own `RetryPolicy(max_attempts=3)`.
- **Temporal UI observability**: every activity is a separate row in the
  workflow history with input / output / wall time / retries.
- **Resumability**: long runs survive process restarts.

The only swap vs. Tier 0 is the YAML's `workflow_type` / `task_queue`
header — the `input:` block is identical.

### Run preview mode via Temporal

#### 1. Start Temporal on the host

```bash
temporal server start-dev
# or
docker run --rm -p 7233:7233 -p 8233:8233 temporalio/auto-setup:1.24
```

UI at `http://localhost:8233`.

#### 2. Install temporal extras

```bash
cd packages/ontology-pipeline
pip install -e ".[temporal]"
```

#### 3. Start the ontology-pipeline worker in one terminal

```bash
export TEMPORAL_TARGET=localhost:7233
python -m ontology_pipeline.temporal.worker \
    --task-queue ontology-pipeline-default
```

The worker registers `OntologyIngestionWorkflow` + every `ontology.*`
activity and waits for jobs. **No Postgres / Qdrant / OpenAI / DeepSeek
required** — preview mode doesn't touch any of them.

#### 4. Submit the preview job from another terminal

```bash
export TEMPORAL_TARGET=localhost:7233   # required for the CLI to find the server
# DEEPSEEK_API_KEY etc. NOT required — preview YAML has every LLM stage off

nexcraft-yaml-job run packages/ontology-pipeline/configs/csod_local_preview_temporal.example.yaml
```

The CLI awaits the workflow result (the example sets `wait_for_result: true`).

#### 5. Inspect

**Temporal UI**: open `http://localhost:8233`, find the workflow run.
Drill in to see:
- `ontology.introspect_source` ran once (against the local schema.sql).
- 11 `ontology.process_one_table` activities ran in parallel (capped by
  `per_table_concurrency: 4`).
- `ontology.run_causal_validation` ran once and returned `{"skipped": 1}`
  with reason "filesystem sink — DB validator unavailable" (preview sink
  doesn't have DB access; the validator post-pass short-circuits cleanly).

**Filesystem**: same `output/preview/` tree as Tier 0:

```bash
tree output/preview -L 3
```

The workflow uses the **exact same** activity bodies the in-process
pipeline uses — including the same `PreviewSink` event-narrative
builders. If something works in Tier 0, it works in Tier 0.5; if you
see a discrepancy, it's a Temporal-layer issue, not a pipeline-layer
issue.

#### 6. Turn on LLM stages incrementally

Same as Tier 0: edit
`configs/csod_local_preview_temporal.example.yaml`, flip
`pipeline.enrich_data_protection: true` (or any other stage), set
`DEEPSEEK_API_KEY` in the worker terminal's environment, re-submit.

The worker picks up the new YAML each time you submit — no restart
needed.

---

### Run preview mode against your own data

Drop a `schema.sql` (pg_dump output) + one CSV per table + an optional
`manifest.json` into any directory:

```
my_data/
├── schema.sql
├── manifest.json   (optional — PK/FK hints)
├── users.csv
├── orders.csv
└── products.csv
```

Then copy `configs/csod_local_preview.example.yaml`, point `source.local`
at your directory, and run.

The SQL parser handles:
- `CREATE TABLE [IF NOT EXISTS] [ONLY] [schema.]table (col type [NOT NULL] [DEFAULT…], …)`
- `COMMENT ON COLUMN schema.table.col IS '…'`
- `COMMENT ON TABLE  schema.table     IS '…'`
- Inline `PRIMARY KEY (cols)` + `REFERENCES schema.table(col)`
- `ALTER TABLE … ADD CONSTRAINT … PRIMARY KEY (…)` / `… FOREIGN KEY (…)`
- Multi-word types (`timestamp with time zone`, `character varying`)
- Doubled-quote escapes (`'It''s tricky'`)

`manifest.json` format (everything is optional):

```json
{
  "tables": {
    "users":  {"pk": "user_id", "role": "employee"},
    "orders": {"pk": "order_id", "fk": ["user_id"]},
    "junction_table": {"pk": ["a_id", "b_id"]}
  }
}
```

---

## Tier 1 — Unit tests

```bash
cd packages/ontology-pipeline
pip install -e ".[dev,llm]"
pytest
```

Expected: **~165 passing, 5 pre-existing failures in `test_enrich.py`**
(documented as a separate follow-up — they predate the foundry-integration
work and are flagged via a spawned task).

The pre-existing failures live in:
- `tests/test_enrich.py::TestRichDescriptionEnricher::test_fills_documentation_block_and_missing_descriptions`
- `tests/test_enrich.py::TestRelationshipInferenceEnricher::test_infers_relationships_for_fk_shaped_columns`
- `tests/test_enrich.py::TestRelationshipInferenceEnricher::test_low_confidence_inference_not_applied_to_mdl`
- `tests/test_enrich.py::TestCausalDependencyEnricher::test_applies_participations_and_emits_candidates`
- `tests/test_enrich.py::TestFailureIsolation::test_llm_failure_returns_warnings_doesnt_raise`

Run a focused slice:

```bash
pytest tests/test_local_preview.py          # SQL parser + CSV loader + PreviewSink + real CSOD E2E
pytest tests/test_annotate_ner.py           # NER + LLM hybrid annotation
pytest tests/test_column_stats.py           # foundry profiling integration
pytest tests/test_cross_asset_causal.py     # cross-asset causal pass
pytest tests/test_causal_validation.py      # statistical causal validation
pytest tests/test_relation_induction.py     # foundry.induce_schema integration
pytest tests/test_temporal_workflow.py      # workflow input shapes + module loading

# ontology-store unit-level (event taxonomy + builders, no DB/Qdrant):
pytest packages/ontology-store/tests/test_vector_events.py
```

---

## Tier 2 — DAO tests against Postgres

Postgres-specific types prevent SQLite stand-ins. The DAO tests are
**skipped** by default and only run when `ONTOLOGY_STORE_TEST_URL` is set.

```bash
# Local Postgres
createdb ontology_test
export ONTOLOGY_STORE_TEST_URL="postgresql+psycopg://$(whoami)@localhost/ontology_test"

cd packages/ontology-store
pytest tests/test_store.py            # spine + annotations
pytest tests/test_cards.py            # card storage (org-scoped)
pytest tests/test_column_stats.py     # table_stat + column_stat
pytest tests/test_relations.py        # relation_type TBox
```

Each test class drops and recreates the schema, so it's safe to point at
a throw-away DB. **Don't** point at production.

---

## Tier 3 — Temporal integration test

The integration test (`tests/test_temporal_workflow.py::TestWorkflowAgainstLocalEnv`)
drives the workflow end-to-end against a real Temporal server, using
**stubbed activities** so it never touches a source database. It validates
fan-out, post-pass ordering, and the terminal `WorkflowSummary` shape.

#### 1. Start Temporal on the host

If you have the `temporal` CLI installed:

```bash
temporal server start-dev
```

Otherwise via Docker:

```bash
docker run --rm -p 7233:7233 -p 8233:8233 temporalio/auto-setup:1.24
```

UI lands at `http://localhost:8233`.

#### 2. Install the temporal extras

```bash
cd packages/ontology-pipeline
pip install -e ".[temporal]"
```

This pulls `temporalio>=1.6` (matches `nexcraft-jobs`).

#### 3. Run the test

```bash
export TEMPORAL_TARGET=localhost:7233
export RUN_TEMPORAL_TESTS=1
pytest tests/test_temporal_workflow.py -v
```

Expected: all 7 unit tests pass + `TestWorkflowAgainstLocalEnv::test_end_to_end_simple_run`
passes (instead of being skipped). The integration test:

- Connects to your host Temporal server.
- Picks a unique task-queue name per run (no collision with other tests
  or a long-running worker).
- Registers stub activities under the same `ontology.*` names the workflow
  uses, so it never touches a real source database.
- Drives the full workflow: introspect → per-table fan-out → post-passes.
- Asserts `tables_processed == 2`, `total_llm_calls == 2`, validation
  post-pass ran.

---

## Run against live Postgres + Qdrant + Temporal

This is the "full E2E" path. Real Postgres data, real ontology-store,
real Qdrant indexer, real Temporal worker, real LLM calls. Useful for:
- Validating new enrichers against representative data.
- Watching the reindex worker pick up events from the queue and append
  them to Qdrant.
- Inspecting Temporal UI for retry behavior on real LLM calls.

### Prerequisites

- **Postgres** running on `localhost:5432`. One database for the source
  (`csod_test`), one for ontology-store (`ontology_store`).
- **Qdrant** running on `localhost:6333` (optional — only needed if you
  want the reindex worker to actually index).
- **Temporal** running on `localhost:7233` (see [step 1 above](#1-start-temporal-on-the-host)).
- **API key** in `$DEEPSEEK_API_KEY` (default) or `$OPENAI_API_KEY`.
- The **CSOD dataset** at the conventional path.

### 1. Load CSOD data into Postgres

The pipeline does NOT own the loader — the data is assumed to be already
in Postgres. Load it once:

```bash
DATA=/Users/sameerm/ComplianceSpark/byziplatform/unstructured/nexcraft/data/indexingsamples

createdb csod_test
psql -d csod_test -f "$DATA/schema.sql"

for table in causal_training_events curriculum_structure_core ou_core \
             training_assignment_core training_assignment_user_core \
             training_core training_local_core training_type_core \
             training_type_local_core transcript_core user_ou_core users_core; do
  echo "Loading $table..."
  psql -d csod_test -c "\COPY public.${table} FROM '$DATA/${table}.csv' WITH (FORMAT csv, HEADER true)"
done
```

### 2. Migrate the ontology-store schema

```bash
createdb ontology_store
export ONTOLOGY_STORE_URL="postgresql+psycopg://$(whoami)@localhost/ontology_store"
cd packages/ontology-store
alembic upgrade head
```

Should print migrations `0001` through `0008`.

### 3. (Optional) Bootstrap Qdrant collections

```python
from ontology_store.vector import (
    HierarchyVectorIndexer, OpenAIEmbedder, get_qdrant_client,
)

client = get_qdrant_client()  # reads QDRANT_URL
embedder = OpenAIEmbedder()    # reads OPENAI_API_KEY
indexer = HierarchyVectorIndexer(qdrant_client=client, embedder=embedder, env="local")

indexer.ensure_all_env_collections()              # hier_t0..t6
indexer.ensure_tenant_collections("csod")          # cards, sql_pairs, historical_qa
                                                   # + causal_events, relation_events,
                                                   # protection_events, card_events
```

### 4. Start the workers

In two terminals:

```bash
# Terminal A — ontology-pipeline Temporal worker
export TEMPORAL_TARGET=localhost:7233
export ONTOLOGY_STORE_URL=postgresql+psycopg://$(whoami)@localhost/ontology_store
export DEEPSEEK_API_KEY=...
python -m ontology_pipeline.temporal.worker --task-queue ontology-pipeline-default

# Terminal B — ontology-store reindex worker (drains the event queue → Qdrant)
export ONTOLOGY_STORE_URL=postgresql+psycopg://$(whoami)@localhost/ontology_store
export QDRANT_URL=http://localhost:6333
export OPENAI_API_KEY=...  # for embeddings
python -m ontology_store.workers.reindex_cli  # (or equivalent)
```

### 5. Submit the example job

In a third terminal:

```bash
export TEMPORAL_TARGET=localhost:7233
export DEEPSEEK_API_KEY=...
export CSOD_PG_USER=$(whoami)
export CSOD_PG_PASSWORD=
export ONTOLOGY_STORE_URL=postgresql+psycopg://$(whoami)@localhost/ontology_store

nexcraft-yaml-job run packages/ontology-pipeline/configs/csod_temporal_job.example.yaml
```

### 6. Inspect

**Temporal UI** at `http://localhost:8233` — workflow + per-activity drilldown.

**Filesystem** (`tee` sink writes both Postgres AND filesystem):

```bash
tree out/csod -L 3
```

**Postgres**:

```sql
SELECT rk, name FROM table_metadata WHERE rk LIKE 'postgres://csod-pg/%';
SELECT predicate, domain, range_type, evidence_count FROM relation_type
WHERE org_id = 'csod' ORDER BY evidence_count DESC;
SELECT asset_rk, predicate, status, validation_diagnostics->>'reason'
FROM causal_candidate ORDER BY updated_at DESC LIMIT 20;
```

**Qdrant** — once the reindex worker drains:

```python
hits = indexer.search_causal_events(
    "csod", query="overdue training drives attrition", k=10,
)
for h in hits:
    print(h.metadata["event_kind"], h.metadata["predicate"], h.text[:80])
```

---

## Common knobs

| Want to | How |
|---------|-----|
| **Inspect what would happen — no backends** | `output.kind: preview` + `source.kind: local_files`. Reads CSVs / SQL; dumps every artifact under `output/preview/`. |
| **Run a single table** | `tables.include: [users_core]` in the YAML. |
| **Skip the LLM stages** | Set the relevant `pipeline.*` flags to `false`. `concepts_source: ner_only` skips the LLM in annotation entirely. |
| **Compute stats but not enrich** | `compute_column_stats: true`, everything else `false`. No LLM needed; runs in seconds against local CSVs. |
| **Re-run after a code change** | Default behavior — content-hash short-circuits unchanged tables. Set `re_enrich_unchanged: true` to force re-enrichment. |
| **Crank parallelism (Temporal mode)** | `per_table_concurrency: 8`. Watch your LLM rate limits. |
| **Switch LLM provider** | Edit `llm.api_key_env` + `llm.model`. Defaults: DeepSeek V3 (`DEEPSEEK_API_KEY` + `deepseek-chat`). Tested alternative: OpenAI (`OPENAI_API_KEY` + `gpt-4o-mini`). |

---

## Troubleshooting

| Symptom | Likely cause |
|---------|--------------|
| `Failed connecting to test server` running Temporal tests | The bundled dev-server can't bind a port (sandboxes, restricted networks). Use a host-provided Temporal server and set `TEMPORAL_TARGET=localhost:7233`. The integration test gates on `TEMPORAL_TARGET` AND `RUN_TEMPORAL_TESTS=1`. |
| `ImportError: No module named 'pandas'` from a test | `compute_column_stats=True` requires pandas. `pip install pandas` or set `compute_column_stats: false`. |
| `psycopg.OperationalError: connection refused` (live mode) | Source / store Postgres isn't running. Test with `psql "$ONTOLOGY_STORE_URL"`. |
| `Environment variable 'DEEPSEEK_API_KEY' referenced but not set` | The YAML uses `${DEEPSEEK_API_KEY}`; either export it or set every LLM stage to `false` in the YAML so no LLM call fires. |
| `RuntimeError: OntologyIngestionWorkflow could not be built` from the worker | `temporalio` isn't installed. `pip install -e ".[temporal]"`. |
| Preview-mode `output/preview/postgres/` is empty | The PG-bound dumps fire only for enrichment stages that PRODUCE PG-bound rows. Turn on at least one of: `compute_column_stats`, `enrich_data_protection`, `infer_relationships`, `enrich_causal_dependencies`, `induce_relation_schema`. |
| `KeyError: 'description'` reading a preview MDL | `description: null` is stripped on serialise (`exclude_none=True`). Use `mdl['models'][0].get('description')`. |
| Temporal-driven preview: worker exits with `TypeError: Activity cannot have keyword-only arguments` | Earlier signature bug, fixed. If you hit it, you're on an older checkout — pull and reinstall: `pip install -e ".[temporal]"`. |
| Temporal-driven preview: workflow runs but `output/preview/` is empty | The worker's CWD differs from where `tree output/preview/` looks. The example YAML uses a relative `base_dir: ./output/preview` — that resolves against the worker's CWD. Either run the worker from the package root, or set an absolute `base_dir`. |
| Temporal-driven preview: `run_causal_validation` returned `skipped` and that's surprising | Expected. Preview sink doesn't have DB access; the validator post-pass short-circuits cleanly. To run the validator, switch `output.kind` to `tee` and provide an `ONTOLOGY_STORE_URL` — that's Tier 4 territory. |

---

## What runs where (mental model — live mode)

```
[Your terminal]
  └── nexcraft-yaml-job run csod_temporal_job.example.yaml
        │
        ▼ submits SubmitJobPayload to Temporal
[Temporal server]  (localhost:7233)
  └── Workflow scheduler dispatches activities to the matching task_queue
        │
        ▼ pulls from queue 'ontology-pipeline-default'
[ontology-pipeline worker]  (long-running, terminal A)
  ├── introspect_source ──→  [Postgres: csod_test]
  ├── process_one_table (xN, parallel) ──→  [Postgres: csod_test]
  │                                  └─ writes to [Postgres: ontology_store]
  │                                                 + [out/csod/]
  │                                                 + enqueues EVENT_* tasks
  ├── run_cross_asset_causal ──→  [DeepSeek] + writes to ontology_store
  ├── run_relation_induction ──→  [DeepSeek] + writes + enqueues EVENT_RELATION
  └── run_causal_validation  ──→  pulls samples from csod_test
                                  writes status + enqueues EVENT_CAUSAL
        │
        ▼ workflow result returned
[Your terminal] sees the WorkflowSummary JSON

[ontology-store reindex worker]  (long-running, terminal B)
  └── drains EVENT_* tasks from reindex_queue (FOR UPDATE SKIP LOCKED)
        ├── hydrates Postgres row by id
        ├── builds event narrative + envelope
        └── appends to the right Qdrant *_events collection
```

## What runs where (mental model — preview mode, in-process)

```
[Your terminal]
  └── ontology-pipeline run --config csod_local_preview.example.yaml
        │
        ▼ same orchestrator, different factories
[in-process pipeline]
  ├── SqlFileIntrospector ──→  [schema.sql + manifest.json on disk]
  ├── per-table loop ─────→
  │     └── TableProfiler (CsvSampleLoader) ──→  [CSV files on disk]
  │     └── enrichers (when LLM stages on) ──→  [DeepSeek]
  │     └── PreviewSink ──→  output/preview/{mdl,postgres,qdrant}/
  └── post-passes ─────→  PreviewSink ──→  output/preview/
        │
        ▼ run() returns; everything inspectable on disk
[Your terminal]
  └── tree output/preview/  (and read JSON files at your leisure)
```

## What runs where (mental model — preview mode, via Temporal)

```
[Your terminal #1]
  └── nexcraft-yaml-job run csod_local_preview_temporal.example.yaml
        │
        ▼ submits to Temporal
[Temporal server]  (localhost:7233)
  └── dispatches to task_queue 'ontology-pipeline-default'
        │
[ontology-pipeline worker]  (terminal #2, long-running)
  ├── ontology.introspect_source  ──→  [schema.sql + manifest.json on disk]
  ├── ontology.process_one_table x11 (parallel, capped by per_table_concurrency)
  │     └── TableProfiler (CsvSampleLoader) ──→  [CSV files on disk]
  │     └── PreviewSink ──→  output/preview/{mdl,postgres,qdrant}/
  ├── ontology.run_cross_asset_causal     (skipped: stage disabled)
  ├── ontology.run_relation_induction     (skipped: stage disabled)
  └── ontology.run_causal_validation      (returns {"skipped": 1}: preview sink)
        │
        ▼ Workflow returns WorkflowSummary
[Your terminal #1] sees the summary
[Temporal UI] shows every activity's history, retries, wall times

[No Postgres / Qdrant / DeepSeek touched]
```

The preview path is faithful: it uses the same builders, same event
envelopes, same enrichment stages. The only swap is the introspector and
the sink. If something works in preview, it works in live mode against
the same backends.
