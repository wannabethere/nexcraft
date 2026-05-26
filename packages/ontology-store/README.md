# ontology-store

Shared persistence layer for the foundry. Holds:

- **SQLAlchemy models** for the spine (amundsenrds-compatible minimal subset)
  plus our extensions (`organization`, `source`, `catalog`, `schema_ext`,
  `table_ext`, `column_ext`, `lineage_edge`, `asset_annotation_provenance`,
  `hierarchy_audit`).
- **DAOs** that hide the SQL behind typed write/read methods:
  - `HierarchyDAO` — writes spine + extensions from an `MDLDocument`.
  - `AnnotationDAO` — writes annotations with the no-clobber + provenance
    semantics from `mdl_table_concept_annotation_spec.md` §5.3.
  - `AssetReader` — read paths used by `ontology-retrieval`.
- **Pydantic schemas** for the wire format shared between pipeline (writer)
  and retrieval (reader): `MDLDocument`, `AssetAnnotations`, `TableContext`,
  `AssetHit`, `RetrievalScope`.

This package is the **single source of truth for storage shapes**. Adding a
column means: model in `db/models.py`, Pydantic in `schemas/`, DAO update,
Alembic revision. Done.

## Install

```bash
cd packages/ontology-store
pip install -e ".[dev]"
```

## Set up the database

```bash
# Provision Postgres (any flavor, 13+ recommended)
createdb ontology_foundry

# Apply the schema
export ONTOLOGY_STORE_URL=postgresql+psycopg://user:pass@localhost:5432/ontology_foundry
alembic upgrade head
```

The single initial migration creates every table at once from the SQLAlchemy
metadata. Subsequent schema changes get incremental Alembic revisions.

## Use it — write side

```python
from ontology_store import (
    Database, HierarchyDAO, AnnotationDAO,
    OrganizationIn, SourceIn, CatalogIn,
    MDLDocument, AssetAnnotations,
)

db = Database.from_env()

with db.session() as s:
    h = HierarchyDAO(s, actor="pipeline@dev")
    h.upsert_organization(OrganizationIn(org_id="acme-corp", display_name="Acme"))
    h.upsert_source(SourceIn(
        source_id="csod-servicenow-local", org_id="acme-corp", kind="postgres",
        instance_name="ServiceNow Local", display_name="ServiceNow Local",
    ))
    # The MDL doc carries the table's full shape; one call upserts spine + extensions.
    h.upsert_mdl_document(my_mdl_doc)

    a = AnnotationDAO(s, actor="pipeline@dev")
    outcomes = a.write(my_annotations)   # {'concepts': 'applied', 'key_areas': 'applied', ...}
```

## Use it — read side

```python
from ontology_store import Database, AssetReader, RetrievalScope

db = Database.from_env()

with db.session() as s:
    r = AssetReader(s)

    # 1. By rk
    ctx = r.get_asset("postgres://csod-servicenow-local.servicenow_db/public/csod_employee")

    # 2. List with filters
    hits = r.list_assets(scope=RetrievalScope(
        org_id="acme-corp",
        source_ids=["csod-servicenow-local"],
        concepts=["employee", "training_assignment"],
        lifecycle_stages=["production"],
    ), limit=20)

    # 3. Search
    hits = r.search_assets(
        query="employee training",
        scope=RetrievalScope(org_id="acme-corp", concepts=["employee"]),
        k=10,
    )
```

## The no-clobber rule in action

```python
# 1. LLM writes annotations
anno_v1 = AssetAnnotations(
    asset_rk="postgres://...csod_employee",
    concepts=["employee"], source="llm_enrichment", confidence=0.78,
)
AnnotationDAO(s).write(anno_v1)
# outcomes: {'concepts': 'applied', 'key_areas': 'noop_empty', 'causal_relations': 'noop_empty'}

# 2. Human corrects
anno_v2 = AssetAnnotations(
    asset_rk="postgres://...csod_employee",
    concepts=["employee", "external_contractor"],
    source="human", written_by="jane.k@acme.com",
)
AnnotationDAO(s).write(anno_v2)
# outcomes: {'concepts': 'applied'}   (human overrides LLM-prior)

# 3. LLM tries again — preserved
anno_v3 = AssetAnnotations(
    asset_rk="postgres://...csod_employee",
    concepts=["employee_only"], source="llm_enrichment", confidence=0.91,
)
AnnotationDAO(s).write(anno_v3)
# outcomes: {'concepts': 'skipped_clobber'}   (preserves Jane's edit)
# An audit row records the would-have-written value for ops review.
```

## Vector layer (optional — `[vector]` extra)

Qdrant-backed vector store + the 10 collections from `hierarchy_persistence_and_ingestion_spec.md`.

Install:

```bash
pip install -e ".[vector,dev]"
```

Components:

| Module | Purpose |
|---|---|
| `ontology_store.vector.QdrantClientFactory` | Cached Qdrant client; reads `QDRANT_URL` / `QDRANT_HOST` / `QDRANT_API_KEY` from env |
| `ontology_store.vector.QdrantDocumentStore` | Legacy-compatible store (`add_documents`, `semantic_search`, `delete_by_project_id`, `.collection` adapter) + new structured API (`upsert_points`, `search`, `count`, `delete_by_filter`) |
| `ontology_store.vector.OpenAIEmbedder` | Default embedder, `text-embedding-3-small` (1536 dim) |
| `ontology_store.vector.HierarchyVectorIndexer` | High-level per-tier upserts + search (one method per tier — `upsert_asset`, `search_cards`, etc.) |
| `ontology_store.vector.collections` | 10 `CollectionSpec` instances + name resolver |

### Collection roster

| `tier_id` | Scope | Name template |
|---|---|---|
| `hier_t0_orgs` | env | `hier_t0_orgs_{env}` |
| `hier_t1_sources` | env | `hier_t1_sources_{env}` |
| `hier_t2_catalogs` | env | `hier_t2_catalogs_{env}` |
| `hier_t3_schemas` | env | `hier_t3_schemas_{env}` |
| `hier_t4_assets` | env | `hier_t4_assets_{env}` |
| `hier_t5_fields` | env | `hier_t5_fields_{env}` |
| `hier_t6_codes` | env | `hier_t6_codes_{env}` |
| `cards` | tenant | `cards_{tenant_id}` |
| `sql_pairs` | tenant | `sql_pairs_{tenant_id}` |
| `historical_qa` | tenant | `historical_qa_{tenant_id}` |

### Use it — write side

```python
from ontology_store.vector import (
    HierarchyVectorIndexer, OpenAIEmbedder, QdrantClientFactory,
)

client = QdrantClientFactory.get()  # reads QDRANT_URL from env
indexer = HierarchyVectorIndexer(qdrant_client=client, embedder=OpenAIEmbedder(), env="prod")

indexer.ensure_all_env_collections()           # one-time bootstrap
indexer.ensure_tenant_collections("acme-corp") # per-tenant bootstrap

# Upsert an asset point (T4)
indexer.upsert_asset(
    "postgres://acme-pg.servicenow_db/public/csod_employee",
    text="Employee master record — identity, role, department, employment_status. ...",
    payload={
        "asset_kind": "table",
        "lifecycle_stage": "production",
        "org_id": "acme-corp",
        "source_id": "csod-servicenow-local",
        "concepts": ["employee"],
        "key_areas": ["Workforce", "Training_Compliance"],
        "causal_relations": ["overdue_risk", "compliance_gap"],
        "primary_object_type": "employee",
    },
)
```

### Use it — read side

```python
# Direct on the indexer
hits = indexer.search_assets(
    "training compliance",
    where={"concepts": ["employee"], "asset_kind": "table"},
    k=10,
)
for h in hits:
    print(h.id, h.score, h.payload.get("metadata", {}).get("concepts"))
```

### Legacy compatibility

`QdrantDocumentStore` exposes the same API as the legacy
`genieml/agents/app/storage/qdrant_store.DocumentQdrantStore`. Drop-in replacement:

```python
# Before
from app.storage.qdrant_store import DocumentQdrantStore
store = DocumentQdrantStore(qdrant_client=client, collection_name="...", embeddings_model=emb)

# After
from ontology_store.vector import QdrantDocumentStore
store = QdrantDocumentStore(qdrant_client=client, collection_name="...", embedder=emb)
# add_documents, semantic_search, delete_by_project_id, .collection — all preserved
```

Filter dicts use the same legacy syntax: `{"$and": [...]}`, `{"key": {"$eq": v}}`,
`{"key": {"$in": [v1, v2]}}`. Plus a new flat shape — `{"concepts": ["a", "b"]}`
translates list values to `MatchAny` semantics for ontology-style array fields.

## Reindex worker (auto-Qdrant from Postgres writes)

`HierarchyDAO.upsert_mdl_document(...)` and `AnnotationDAO.write(...)` both
enqueue a `qdrant_asset` task on `reindex_queue` after commit. A long-running
worker drains the queue, reads the affected row from Postgres, computes the
narrative text + payload per the tier's collection spec, and upserts via
`HierarchyVectorIndexer`.

```
DAO write → reindex_queue (task=qdrant_asset, payload={asset_rk})
                                │
                                ▼
                      ReindexWorker.run_forever()
                                │
                                ▼  for each pending task
              ┌─────────────────────────────────────┐
              │ AssetReader.get_asset(rk)            │  read full TableContext
              │ build_asset_narrative + payload     │  per collection spec
              │ HierarchyVectorIndexer.upsert_asset │  Qdrant upsert
              │ QueueDAO.mark_done(queue_id)         │
              └─────────────────────────────────────┘
```

### Run it

```bash
# Long-running (in a service / docker container)
export ONTOLOGY_STORE_URL=postgresql+psycopg://user:pass@localhost/ontology_foundry
export QDRANT_URL=http://localhost:6333
export OPENAI_API_KEY=sk-...

ontology-store reindex run-forever --env prod --batch 10 --poll 2.0

# Run once (cron / tests)
ontology-store reindex run-once --limit 50

# Inspect queue
ontology-store reindex status
#   pending  : 12
#   running  : 0
#   done     : 4128
#   failed   : 0
```

### Properties

- **Idempotent**: same task processed twice produces the same Qdrant point.
- **Resumable**: workers crash-resume from `pending` rows on next poll.
- **Retry-safe**: failures increment `attempts`; row resets to `pending` until
  `max_attempts` is hit, then moves to `failed`. `last_error` carries the last
  exception message.
- **Multi-worker safe**: `dequeue_batch` uses `FOR UPDATE SKIP LOCKED` so N
  workers process disjoint batches without contention.
- **Live-sync ready**: the live-sync worker (when wired) enqueues
  `qdrant_asset` tasks on source-change events; same worker drains them.

### Handler coverage in v1

| Task kind | Handler | Status |
|---|---|---|
| `qdrant_asset` | `AssetReader.get_asset` → `upsert_asset` | ✓ active |
| `qdrant_source` | spine `Source` → `upsert_source` | ✓ active |
| `qdrant_schema` | spine `SchemaMetadata` + `SchemaExt` → `upsert_schema` | ✓ active |
| `qdrant_card` | requires body+kind in payload → `upsert_card` | ✓ active when callers pass full payload |
| `qdrant_field` | T5 reindex | stub (deferred until column_ext + descriptions are joined) |
| `qdrant_catalog` | T2 reindex | stub |
| `bundle_asset` / `bundle_catalog` / `lineage_derive` / `annotation_enrich` | non-Qdrant cascade tasks | not handled by this worker (separate worker classes will live alongside) |

## What's NOT in v1

- **Cards table / card_ref table** — semantic-layer card storage. Cards
  currently live on disk; promotion to Postgres happens in a subsequent revision.
- **The full T6 (code_list / code_value)**, function/metric/api_endpoint
  asset subtypes, lineage publish-side tables (`publisher_state`,
  `publisher_drift`), sync meta tables (`source_sync_state`,
  `asset_sync_state`). Roadmap.
- **Non-Qdrant workers** — bundle-emit, reconciler, audit-pruner all referenced
  by `hierarchy_persistence_and_ingestion_spec.md` §14 but not implemented.
