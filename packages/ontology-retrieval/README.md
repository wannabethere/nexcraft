# ontology-retrieval

Two surfaces over the same ontology-store, sharing one internal pipeline:

1. **Internal Python pipeline** — `ontology_retrieval.pipeline.RetrievalPipeline`. The replacement for the genieml-agents `RetrievalHelper` / `RetrievalPipeline` once the ontology store is populated. Importable, registry-driven, modular by source.

2. **HTTP search APIs** — narrow purpose-built endpoints (`/assets/*`, `/lineage/trace`) that internally call the pipeline. The pipeline itself is **not** exposed as a generic HTTP endpoint.

```
                        ┌───────────────────────────────────┐
genieml-agents (future) │                                   │
compliance-skill        │   LegacyRetrievalPipeline (compat)│ ◄── stand-in for the legacy API
mcp-server (future)     │   LegacyRetrievalHelper           │     when ontology is ready
                        └─────────────────┬─────────────────┘
                                          │
HTTP clients ──► /assets/*, /lineage/trace ──┐
                                             │
                                             ▼
                              ┌─────────────────────────────┐
                              │   RetrievalPipeline         │  internal Python
                              │   ─────────────────         │
                              │   • registry of kinds       │
                              │   • named source map        │
                              │   • async LRU cache         │
                              └─────────────┬───────────────┘
                                            │
                                            ▼
                                   Sources (Postgres, Qdrant)
                                            │
                                            ▼
                                      ontology-store
```

## HTTP surface (the only API endpoints)

| Method + path | Pipeline kind | Purpose |
|---|---|---|
| `GET  /health` | — | Liveness |
| `GET  /health/db` | — | DB connectivity |
| `GET  /assets/by-rk?rk=...` | `asset_by_rk` | Hydrate one asset |
| `POST /assets/list` | `asset_list` | Filtered enumeration |
| `POST /assets/search` | `asset_search` | Query + concept/key_area-aware ranking |
| `POST /lineage/trace` | `lineage_trace` | Multi-hop walk of `lineage_edge` |

The HTTP layer is a thin transformation: request → pipeline call → response. No business logic, no source coupling. Adding a new HTTP endpoint = (a) ensure a kind exists, (b) write a 30-line route handler that calls `pipeline.run(kind, ...)`.

## Internal pipeline (the Python import surface)

Registered kinds (active + stub):

| Kind | Status | Sources required | Aliases |
|---|---|---|---|
| `asset_search` | active | postgres_assets | `database_schemas`, `views` |
| `asset_by_rk` | active | postgres_assets | — |
| `asset_list` | active | postgres_assets | — |
| `lineage_trace` | active | postgres_lineage | `lineage_upstream`, `lineage_downstream` |
| `sql_pairs_search` | stub | — | `sql_pairs` |
| `instructions_search` | stub | — | `instructions` |
| `historical_qa_search` | stub | — | `historical_questions` |
| `cards_search` | stub | — | — |
| `metrics_search` | stub | — | `metrics` |
| `claims_by_asset` | stub | — | — |

Stubs return `data: []` + `metadata.stub: true` + roadmap note. When their backing storage lands, only the fetcher implementation changes — same kind id, same input schema, callers don't change.

### Adding a kind

```python
from ontology_retrieval.pipeline import (
    RetrievalContext, RetrievalKind, RetrievalResult, register_kind,
)
from pydantic import BaseModel

class MyKindIn(BaseModel):
    query: str
    k: int = 10

def _fetch(ctx: RetrievalContext) -> RetrievalResult:
    src = ctx.source("postgres_assets")           # named source from the map
    hits = src.search_assets(query=ctx.input.query, scope=..., k=ctx.input.k)
    return RetrievalResult(kind=ctx.kind, data=[h.model_dump() for h in hits])

register_kind(RetrievalKind(
    id="my_kind",
    description="...",
    input_schema=MyKindIn,
    sources_required=("postgres_assets",),
    fetcher=_fetch,
    cache_ttl_seconds=300,
))
```

## Stand-in replacement for genieml-agents / compliance-skill

When the ontology store is populated, swap the callers' imports — no code change beyond the import.

### Migration shape — pipeline-level (`RetrievalPipeline`)

Before (in `app/agents/pipelines/retrieval_pipeline.py`):

```python
from app.agents.pipelines.base import AgentPipeline
from app.agents.retrieval.retrieval_helper import RetrievalHelper

class RetrievalPipeline(AgentPipeline):
    async def run(self, retrieval_type, **kwargs):
        result = await self._retrieval_helper.get_database_schemas(...)
        return {"formatted_output": {"documents": ...}, "metadata": ...}
```

After (the import-only swap):

```python
from ontology_retrieval.compat import build_legacy_pipeline
from ontology_store import Database

pipeline = build_legacy_pipeline(
    default_org_id="acme-corp",
    database=Database.from_env(),
)
result = await pipeline.run("database_schemas", query="employee training", project_id="csod_risk_attrition")
docs = result["formatted_output"]["documents"]
```

The `LegacyRetrievalPipeline.run(retrieval_type, **kwargs)` signature is identical:

- `retrieval_type` accepts all legacy values (`database_schemas`, `sql_pairs`, `instructions`, `historical_questions`, `views`, `metrics`) via alias resolution.
- `project_id=...` translates to `scope.legacy_project_id` automatically.
- `top_k=...` / `max_retrieval_size=...` normalize to `k`.
- `similarity_threshold=...` and other obsolete kwargs are silently dropped (ranking is now owned internally).
- Return shape is `{"formatted_output": {"documents": [...]}, "metadata": {...}}` — byte-compatible with the legacy contract.

### Migration shape — helper-level (`RetrievalHelper`)

For code that calls helper methods directly:

```python
from ontology_retrieval.compat import LegacyRetrievalHelper
from ontology_retrieval.pipeline import build_pipeline_from_config
from ontology_store import Database

pipeline = build_pipeline_from_config(database=Database.from_env())
helper = LegacyRetrievalHelper(pipeline=pipeline, default_org_id="acme-corp")

result = await helper.get_database_schemas(project_id="csod_risk_attrition", query="employee")
schemas = result["schemas"]
```

Available methods (same signatures as legacy):
- `get_database_schemas(project_id, query, ...)`
- `get_views(project_id, query, ...)`
- `get_metrics(project_id, query, ...)`
- `get_sql_functions(query, project_id, ...)`
- `get_sql_pairs(query, project_id, ...)`
- `get_instructions(query, project_id, ...)`
- `get_historical_questions(query, project_id, ...)`
- `get_lineage(asset_rk, direction, ...)` — *new; not in legacy*

The genieml-agents and compliance-skill packages are **not** touched in this version. The migration is one-PR-per-caller-site once the data is in place.

## Install + run the HTTP service

```bash
cd packages/ontology-retrieval
pip install -e ".[dev]" -e "../ontology-store[dev]"

export ONTOLOGY_STORE_URL=postgresql+psycopg://user:pass@localhost/ontology_foundry

ontology-retrieval serve --host 0.0.0.0 --port 8088
```

```bash
# Try the search APIs
curl -s 'http://localhost:8088/assets/by-rk?rk=postgres://acme-pg.servicenow_db/public/csod_employee'

curl -s -X POST http://localhost:8088/assets/search \
  -H 'content-type: application/json' \
  -d '{
        "query": "employee training",
        "scope": {
          "org_id": "acme-corp",
          "concepts": ["employee", "training_assignment"]
        },
        "k": 10
      }'

curl -s -X POST http://localhost:8088/lineage/trace \
  -H 'content-type: application/json' \
  -d '{
        "asset_rk": "postgres://acme-pg.servicenow_db/public/csod_employee",
        "direction": "upstream",
        "max_hops": 2
      }'
```

## Config — declarative source bindings

`configs/retrieval_pipeline.yaml` is loaded at app startup if provided to `create_app()`. Otherwise `default_config()` wires the two Postgres-backed sources.

```yaml
sources:
  - name: postgres_assets
    kind: postgres_asset
  - name: postgres_lineage
    kind: postgres_lineage
  # Uncomment when Qdrant is wired
  # - name: qdrant_hier_t4_assets
  #   kind: qdrant
  #   options: { collection: hier_t4_assets_prod }

kinds:
  # Per-kind cache TTL overrides (optional)
  # - id: asset_search
  #   cache_ttl_seconds: 1200

default_cache_ttl_seconds: 600
cache_max_entries: 1024
cache_enabled: true
```

## Tests

```bash
pytest tests/
```

`tests/test_pipeline.py` — registry shape, alias resolution, cache hits, stub behavior, source-missing errors. No live DB / Qdrant required.

`tests/test_compat.py` — verifies the stand-in semantics: `project_id` → scope, `top_k`/`max_retrieval_size` → `k`, return-shape parity, alias routing, dropped legacy kwargs.

## Retrieval evaluation (background)

Two scoring modes wired in v1:

1. **`historical_comparison`** — deterministic. Compares retrieved results to
   `eval_case.expected_asset_rks` and computes:
   precision@1/3/5/10, recall@5/10, MRR, nDCG@5/10, hit_rate, forbidden_violations.
   Pass gate (default): `hit_rate == 1 AND forbidden_violations == 0`. Tightenable
   with `pass_min_recall_at_5` / `pass_min_mrr`.

2. **`llm_judge`** — LLM-as-judge. Each retrieved item gets a 0–2 relevance
   rating + holistic 0–5 coverage rating + missing-concepts list. Aggregated
   into `judge_mean_rating`, `judge_relevant_rate`, `judge_coverage_rate`, etc.
   Pass gate (default): `coverage_rating >= 3 AND judge_relevant_rate >= 0.3`.

### Background worker pattern

```
operator / cron ──► INSERT INTO eval_run (status='pending', ...)
                              │
                              ▼
                    EvalWorker.run_pending()                   ← long-running or cron
                              │
                              ▼  per pending run:
              ┌─────────────────────────────────────┐
              │ FOR UPDATE SKIP LOCKED → mark running│
              │ load eligible eval_case rows         │
              │ for each case:                       │
              │   pipeline.run(kind, query, scope)  │
              │   for each scorer:                   │
              │     scorer.score(...)                │
              │     INSERT eval_result               │
              │ aggregate → INSERT eval_metric rows │
              │ mark done                            │
              └─────────────────────────────────────┘
```

### Tables

| Table | Purpose |
|---|---|
| `eval_case` | curated question + `expected_asset_rks` + scope; the ground truth |
| `eval_run` | one execution; status pending/running/done/failed |
| `eval_result` | per (run, case, scorer) — retrieved_rks + metrics + llm_judgment + pass_gate |
| `eval_metric` | rolled-up aggregates (mean_precision_at_5, mean_mrr, pass_rate, ...) per run × scorer |

Migration: `alembic upgrade head` applies the new `0003_add_eval_tables`.

### CLI

```bash
# Import curated cases from YAML
ontology-retrieval eval import-cases --path eval_cases.yaml

# Enqueue a run (worker picks it up)
ontology-retrieval eval enqueue \
  --kind asset_vector_search \
  --scorers historical_comparison,llm_judge \
  --hardness medium,hard

# Or run immediately (creates + executes the run row)
ontology-retrieval eval run-once --kind asset_search --scorers historical_comparison

# Background loop (deploy as a service / cron-friendly)
ontology-retrieval eval run-pending --limit 5 --with-llm-judge

# Status
ontology-retrieval eval status
#   pending  : 1
#   running  : 0
#   done     : 42
#   failed   : 0
#
# Latest run: id=42 kind=asset_vector_search status=done cases=50 passed=47
```

### Example `eval_cases.yaml`

```yaml
cases:
  - case_id: q001_training_to_attrition
    question: Why might increased training completion rates correlate with reduced attrition in clinical departments?
    intent: causal_reasoning
    expected_anchors:
      - employee
      - training_assignment
      - compliance_gap
    expected_asset_rks:
      - postgres://csod-servicenow-local.servicenow_db/public/csod_employee
      - postgres://csod-servicenow-local.servicenow_db/public/training_assignment
    forbidden_asset_rks: []
    scope_payload:
      org_id: acme-corp
      concepts: [employee, training_assignment]
      key_areas: [Workforce, Training_Compliance]
    retrieval_kind_default: asset_vector_search
    hardness: medium
    domain_tags: [HR, Clinical, Compliance]
    enabled: true
    authored_by: jane.k@acme.com
```

### Authoring scorer config per run

Use `case_filter` in the `eval_run` row (or `--case-ids` / `--hardness` on the
CLI) to scope which cases run. Use `scorer_names` to choose which scorers
execute; the worker constructs them from a config map. Both scorers can run
together — each produces its own `eval_result` row with its own pass gate.

### Programmatic use

```python
from ontology_store import Database
from ontology_retrieval.eval import (
    EvalWorker, HistoricalComparisonScorer, LLMJudgeScorer,
)
from ontology_retrieval.pipeline import build_pipeline_from_config, default_config

db = Database.from_env()
pipeline = build_pipeline_from_config(default_config(), database=db)

scorers = [
    HistoricalComparisonScorer(pass_min_recall_at_5=0.6),
    LLMJudgeScorer(openai_model="gpt-4o-mini", pass_min_coverage=3),
]
worker = EvalWorker(database=db, pipeline=pipeline, scorers=scorers)

# Execute one specific run
worker.execute_run(run_id=42)

# Or poll for pending
stats = worker.run_pending(limit=5)
```

### Querying results

```sql
-- Aggregate metrics for the most-recent run per retrieval_kind
SELECT r.retrieval_kind, m.scorer_name, m.metric_name, m.metric_value, m.cardinality
FROM eval_metric m
JOIN eval_run r ON r.run_id = m.run_id
WHERE r.run_id = (
    SELECT MAX(run_id) FROM eval_run WHERE retrieval_kind = 'asset_vector_search' AND status = 'done'
)
ORDER BY m.scorer_name, m.metric_name;

-- Per-case results for a run (drill-down)
SELECT case_id, scorer_name, pass_gate, metrics, llm_judgment->>'rationale' AS rationale
FROM eval_result WHERE run_id = 42 ORDER BY case_id, scorer_name;

-- Pass rate trend across the last 10 runs
SELECT r.run_id, r.started_at, m.scorer_name, m.metric_value AS pass_rate
FROM eval_metric m JOIN eval_run r USING (run_id)
WHERE m.metric_name = 'pass_rate' AND r.status = 'done'
ORDER BY r.started_at DESC LIMIT 10;
```

### Properties

- **Idempotent** — re-running a run produces deterministic `historical_comparison`
  metrics; `llm_judge` has minor variance per LLM call.
- **Multi-worker safe** — `_claim_runs` uses `FOR UPDATE SKIP LOCKED`.
- **Recoverable** — failed runs leave `last_error` in `eval_run`; operator can
  inspect, fix, then `UPDATE eval_run SET status='pending' WHERE run_id=…` to retry.
- **Composable** — same `Scorer` Protocol; adding (e.g.) a `groundedness` scorer
  later is a new class + registration.

## Roadmap

| Item | Status |
|---|---|
| Wire real Qdrant client in `QdrantSource` for vector-backed search | Pending |
| Populate `cards_search` (requires card storage tables) | Pending |
| Populate `sql_pairs_search` (requires `sql_pair` table + Qdrant collection) | Pending |
| Populate `metrics_search` (requires `metric_metadata` subtype) | Pending |
| Populate `claims_by_asset` (requires `claim` + `causal_candidate` tables) | Pending |
| Auth + tenant scoping | Pending |
| Caller-side migration of genieml-agents / compliance-skill | Pending — separate PRs once data is ready |
