# Hierarchy Persistence and Ingestion — Specification

**Status:** Draft 2026-05-15.
**Part of:** Data Knowledge Hierarchy series.
**Depends on:** `T0_T1_organization_source_spec.md`, `T2_to_T6_amundsenrds_sidecar_spec.md`, `semantic_layer_card_spec.md`, `mdl_bundle_spec.md`.
**Forward refs:** `bundle_publishers_spec.md`, `bundle_consumer_api_spec.md`, `evaluation_harness_spec.md`.
**Leverages:**
- [`amundsendatabuilder`](https://github.com/amundsen-io/amundsendatabuilder) — vendor extractors and the Extractor/Transformer/Loader pipeline shape.
- [`amundsenrds`](https://github.com/amundsen-io/amundsenrds) @ commit `4509bb0` — Alembic migrations + ORM models.
- `ontology_foundry.llm` — provider abstraction for any LLM-assisted enrichment steps.
- `ontology_foundry.context.TabularContextBundle` — asset profiling consumed by the MDL emitter.
- `ontology_foundry.eval.gates` — card validation gates run at write time.

---

## 1. Scope

This spec defines:

1. The **operational contract** (`HierarchyStore`) the rest of the system uses to read and write hierarchy state.
2. The **write-through pattern** that keeps Postgres (SoR), Qdrant (search index), and bundle files (wire format) consistent.
3. The **ingestion pipelines** built on `amundsendatabuilder` that pull metadata from data sources and load it into the hierarchy.
4. The **bundle emission pipeline** that materializes per-asset bundles from storage.
5. The **audit, retry, and error semantics** of all writes.
6. The **migration model** (two Alembic heads — amundsenrds upstream + ours).

Out of scope:
- Bundle file formats (in `mdl_bundle_spec.md`).
- Publisher mappers (in `bundle_publishers_spec.md`).
- Consumer query API (in `bundle_consumer_api_spec.md`).
- Evaluation harness (in `evaluation_harness_spec.md`).

---

## 2. Stores

| Store | Authoritative for | Read pattern | Write pattern |
|---|---|---|---|
| **Postgres** (amundsenrds + sidecars + our tables + cards) | Spine data, knowledge wrappers, ontology bindings, audit, card index | Direct SQL / SQLAlchemy ORM | Transactional |
| **Qdrant** | Semantic search over narrative fields | Vector + payload filter | Eventually consistent; enqueued from Postgres write |
| **Filesystem** — `tenants/<org_id>/semantic_layer/` | Cards (source of truth for card content) | File read | File write (git-reviewable) |
| **Filesystem** — `tenants/<org_id>/assets/...` and `tenants/<org_id>/catalogs/...` | Bundles (derived artifacts) | File read | File write (atomic regenerate) |

Postgres is the commit point for non-card data. The filesystem is the commit point for cards (followed by Postgres mirror).

---

## 3. `HierarchyStore` contract

The Python interface every internal subsystem uses. Implementations are stateful (hold DB and Qdrant clients).

```python
class HierarchyStore(Protocol):

    # ---- T0 ----
    def create_org(self, org: OrganizationIn, *, actor: str) -> OrgId: ...
    def get_org(self, org_id: OrgId) -> Organization: ...
    def update_org(self, org_id: OrgId, patch: dict, *, actor: str) -> None: ...
    def append_operating_region(self, org_id: OrgId, region: RegionIn, *, actor: str) -> None: ...
    def deactivate_operating_region(self, org_id: OrgId, region_id: str, *, actor: str) -> None: ...

    # ---- T1 ----
    def create_source(self, source: SourceIn, *, actor: str) -> SourceId: ...
    def get_source(self, source_id: SourceId) -> Source: ...
    def update_source(self, source_id: SourceId, patch: dict, *, actor: str) -> None: ...
    def append_entity_claim(self, source_id: SourceId, entity: str, kind: str,
                            declared_by: str, *, actor: str) -> None: ...
    def deactivate_entity_claim(self, source_id: SourceId, claim_id: int, *, actor: str) -> None: ...
    def list_sources(self, org_id: OrgId, *, filters: SourceFilter | None = None) -> list[Source]: ...
    def search_sources(self, query: str, *, org_id: OrgId | None = None,
                       role: str | None = None, kind: str | None = None,
                       k: int = 10) -> list[SourceHit]: ...

    # ---- T2 ----
    def upsert_catalog(self, catalog: CatalogIn, *, actor: str) -> CatalogUid: ...
    def get_catalog(self, catalog_uid: CatalogUid) -> Catalog: ...
    def bind_schema_to_catalog(self, schema_rk: str, catalog_uid: CatalogUid, *, actor: str) -> None: ...

    # ---- T3 ----
    def upsert_schema_ext(self, schema_rk: str, ext: SchemaExtIn, *, actor: str) -> None: ...
    def get_schema(self, schema_rk: str) -> SchemaView: ...   # joins amundsenrds + schema_ext + catalog
    def append_schema_domain_tag(self, schema_rk: str, tag: str, *, actor: str) -> None: ...

    # ---- T4 / T5 ----
    def upsert_table_ext(self, table_rk: str, ext: TableExtIn, *, actor: str) -> None: ...
    def upsert_column_ext(self, column_rk: str, ext: ColumnExtIn, *, actor: str) -> None: ...
    def upsert_api_endpoint(self, endpoint: ApiEndpointIn, *, actor: str) -> str: ...
    def upsert_api_field(self, field: ApiFieldIn, *, actor: str) -> str: ...
    def upsert_function(self, function: FunctionIn, *, actor: str) -> str: ...
    def upsert_metric(self, metric: MetricIn, *, actor: str) -> str: ...

    # ---- T6 ----
    def upsert_code_list(self, code_list: CodeListIn, *, actor: str) -> str: ...
    def upsert_code_value(self, code_value: CodeValueIn, *, actor: str) -> str: ...

    # ---- Lineage ----
    def upsert_lineage_edge(self, edge: LineageEdgeIn, *, actor: str) -> int: ...
    def deactivate_lineage_edge(self, edge_id: int, *, actor: str) -> None: ...
    def list_lineage(self, *, rk: str, direction: str = "both") -> list[LineageEdge]: ...

    # ---- Search / resolve ----
    def search_assets(self, query: str, *,
                      org_id: OrgId | None = None,
                      asset_kind: str | None = None,
                      lifecycle_stage: str | None = None,
                      domain_tags: list[str] | None = None,
                      k: int = 10) -> list[AssetHit]: ...
    def resolve_asset_effective(self, asset_rk: str) -> EffectivePolicy: ...

    # ---- Bindings ----
    def upsert_semantic_bindings(self, bindings: SemanticBindings, *, actor: str) -> None: ...
    def get_semantic_bindings(self, asset_rk: str) -> SemanticBindings | None: ...

    # ---- Cards (delegates to CardStore from semantic_layer_card_spec §14) ----
    @property
    def cards(self) -> CardStore: ...

    # ---- Bundle emission ----
    def emit_bundle(self, asset_rk: str, *, actor: str = "system") -> BundleEmitResult: ...
    def emit_catalog_index(self, catalog_uid: CatalogUid, *, actor: str = "system") -> CatalogIndexEmitResult: ...
```

### 3.1 Patch semantics

`update_*` takes a partial dict. Only listed fields are updated. Each changed field writes a `hierarchy_audit` row with old/new values.

### 3.2 Append vs upsert vs deactivate

- **Append** — for object arrays (operating regions, entity authority claims). Creates a child row. Audit row records the addition.
- **Deactivate** — flips an `active boolean` on a child row. Soft-delete, preserves provenance. Audit row records the deactivation.
- **Upsert** — for entities keyed by stable id. Idempotent. Writes a single row; audit captures the field-level diff.

Hard-delete is not exposed on the contract. Removal is via deactivation or lifecycle-stage transition to `removed`.

### 3.3 `actor`

Every mutation requires an `actor` string (email, service identity, or `system` for pipeline-driven writes). Recorded in `hierarchy_audit`. Used by the bundle's `governance.json` for provenance traceability.

---

## 4. Write-through pattern

Every `HierarchyStore` write follows the same sequence:

```
1. Validate (gates / schema / FK consistency)         — fail-fast
2. Begin Postgres transaction
3. Write the data row(s)
4. Write the audit row(s)
5. Commit
6. (post-commit) Enqueue Qdrant reindex tasks
7. (post-commit) Enqueue bundle regeneration tasks
8. (post-commit) Enqueue downstream lineage-edge derivation tasks (if applicable)
9. Return to caller (success)
```

Postgres commit is the success boundary. Steps 6–8 are best-effort; failures are retried by background workers, not by the caller. SoR is always correct; Qdrant and bundles are eventually consistent.

### 4.1 Reindex queue

Implementation: a Postgres-backed work queue table (`reindex_queue`) consumed by a worker. Keeps everything in one stack — no Redis/Kafka required for this layer.

```sql
CREATE TABLE reindex_queue (
  queue_id      bigserial PRIMARY KEY,
  task_kind     text NOT NULL,                  -- 'qdrant_card'|'qdrant_asset'|'bundle_asset'|'bundle_catalog'|'lineage_derive'
  payload       jsonb NOT NULL,                 -- task-specific (rk, tier, etc.)
  enqueued_at   timestamptz NOT NULL DEFAULT now(),
  started_at    timestamptz,
  completed_at  timestamptz,
  attempts      integer NOT NULL DEFAULT 0,
  last_error    text,
  status        text NOT NULL DEFAULT 'pending' -- 'pending'|'running'|'done'|'failed'
);

CREATE INDEX idx_reindex_pending ON reindex_queue (status, enqueued_at) WHERE status = 'pending';
```

Worker behavior:
- Long-poll for `pending`, pick by `enqueued_at`.
- Mark `running`, increment `attempts`.
- Execute task; on success mark `done`, on failure log error and reset to `pending` with backoff (exponential, max 5 attempts).
- After 5 failures, mark `failed` and surface to ops dashboard.

### 4.2 Idempotency

All reindex tasks must be idempotent. Re-running a Qdrant upsert produces the same point; re-running a bundle emission produces the same files. Workers may dedupe queue entries by `(task_kind, payload.rk)` to coalesce bursts.

### 4.3 Ordering guarantees

Within a single `rk` the queue is processed in enqueue order (queue-level FIFO with worker affinity by rk-hash). Across rks no ordering guarantee. Bundle regeneration tasks may observe a slightly-stale neighbor; the next regeneration converges.

---

## 5. Qdrant indexing

### 5.1 Collections

| Collection | Scope | Point id |
|---|---|---|
| `hier_t0_orgs_<env>` | Organizations | `org_id` |
| `hier_t1_sources_<env>` | Sources | `source_id` |
| `hier_t2_catalogs_<env>` | Catalogs | `catalog_uid` |
| `hier_t3_schemas_<env>` | Schemas | `schema_rk` |
| `hier_t4_assets_<env>` | All asset subtypes (single, with `asset_kind` filter) | `rk` |
| `hier_t5_fields_<env>` | All field subtypes | `rk` |
| `hier_t6_codes_<env>` | Code lists + values | `rk` |
| `cards_<tenant_id>` | Semantic-layer cards (per-tenant; from card spec §9.3) | `{tenant}::{layer}::{kind}::{id}` |

### 5.2 Per-collection embedding text

| Collection | Concatenation source |
|---|---|
| `hier_t0_orgs` | `display_name` + `business_context` + `industry` + `sub_industry` |
| `hier_t1_sources` | `display_name` + `purpose` + `business_context` + `role` + active entity-claim entities |
| `hier_t2_catalogs` | `display_name` + `description` + `purpose` + `notes` |
| `hier_t3_schemas` | `display_name` + `description` (amundsenrds) + `purpose` + `domain_tags` |
| `hier_t4_assets` | name + description (amundsenrds `*_description.description` for the kind) + `purpose` + view DDL summary (if view) + first paragraph of any bound `object_type` card body |
| `hier_t5_fields` | name + description + `semantic_unit` + first sentence of any bound `card_field` mention |
| `hier_t6_codes` | code-list `name` + `description` + value labels (joined) |
| `cards_<tenant>` | card body + `aliases` |

For T4 assets, including a snippet of bound card body strengthens cross-source search ("which assets are employees?" finds tables bound to the `employee` card across sources).

### 5.3 Payload schema (T4 example)

```json
{
  "asset_kind": "table",
  "lifecycle_stage": "production",
  "effective_sensitivity_class": "confidential",
  "effective_pii_categories": ["names","contact"],
  "domain_tags": ["Clinical","Compliance"],
  "org_id": "acme-corp",
  "source_id": "acme-snowflake-prod",
  "catalog_uid": "...",
  "schema_rk": "...",
  "primary_object_type": "encounter",     // from bindings, when present
  "implements_interfaces": ["auditable"],
  "last_indexed_at": "2026-05-15T..."
}
```

Filters used in practice: `asset_kind`, `lifecycle_stage`, `domain_tags`, `effective_sensitivity_class`, `primary_object_type`.

### 5.4 Re-embed vs payload-only update

- **Re-embed** when any narrative source field changed (description, purpose, business_context, bound card body).
- **Payload-only `set_payload`** when only structured fields changed (lifecycle_stage flip, owner change, sensitivity override).

The reindex worker decides which path by comparing the new `sha256(narrative_text)` against the row's prior recorded hash (stored in `qdrant_sync_state`, §5.5).

### 5.5 `qdrant_sync_state`

```sql
CREATE TABLE qdrant_sync_state (
  collection         text NOT NULL,
  point_id           text NOT NULL,
  narrative_hash     text,
  payload_hash       text,
  last_indexed_at    timestamptz NOT NULL,
  last_attempt_at    timestamptz NOT NULL,
  last_error         text,
  PRIMARY KEY (collection, point_id)
);
```

Tracks per-point indexing state. The reindex worker reads this to decide what changed and what to do.

---

## 6. Bundle emission

### 6.1 Trigger matrix

Per `mdl_bundle_spec.md` §11. Translated to specific queue tasks:

| Postgres event | Queue task |
|---|---|
| `table_metadata` / `table_ext` / `column_metadata` / `column_ext` change for asset X | `bundle_asset` with `payload.asset_rk = X` |
| `api_endpoint_metadata` / `api_field_metadata` change | `bundle_asset` |
| `function_metadata` / `function_parameter` change | `bundle_asset` |
| `metric_metadata` / `metric_dimension` change | `bundle_asset` (the metric) + `bundle_asset` (each `depends_on` asset, for its `metrics.json`) |
| `organization` / `source` change | Walk all assets owned by source; enqueue `bundle_asset` for each (for `context.json` regeneration) |
| `catalog` change | Walk all assets in catalog's schemas; enqueue `bundle_asset` for each |
| `schema_ext` change | Walk all assets in schema; enqueue `bundle_asset` for each |
| Card edit (body hash change) | For every asset with `semantic_bindings.json` referencing the card: enqueue `bundle_asset` + binding-validator task |
| Asset added/removed/renamed in catalog | `bundle_catalog` for that catalog's index |
| Lineage edge added/removed | `bundle_asset` on both endpoints |
| Foundry pipeline produces new claim/candidate | `bundle_asset` for each asset the claim/candidate references |
| Owner / follower / usage record change | `bundle_asset` (governance.json only) |

The worker may coalesce concurrent tasks for the same `rk` to one emission (debounce window 30s).

### 6.2 Emission steps

For one `bundle_asset` task:

1. Read all upstream rows:
   - amundsenrds: `table_metadata` (and descriptions) for tabular; `api_endpoint_metadata` (and descriptions) for API; etc.
   - Sidecars: `table_ext`, `column_ext`, `schema_ext`, etc.
   - Source + Catalog + Organization chain.
   - Effective values from `v_asset_effective`.
   - Lineage edges from `lineage_edge`.
   - Owner / follower / usage from `v_asset_owner`, `v_asset_usage`.
   - Bound `semantic_bindings.json` (from `semantic_bindings` table; §7).
   - Causal claims / candidates referencing this asset.
   - Metrics where this asset is `primary_asset_rk` or in `depends_on`.
2. Build each file in memory (`mdl.json`, `context.json`, `semantic_bindings.json`, `governance.json`, `causal.json`, `metrics.json`, `bundle_manifest.json`).
3. Compute the manifest's per-file sha256 + `concern_version`.
4. Write to a sibling temp directory: `assets/<source>/<schema>/<asset_name>.tmp.<pid>.<ts>/`.
5. `fsync` each file.
6. Atomic rename of the temp directory over the live one (`renameat2` on Linux, atomic on the same filesystem).
7. Update `bundle_emit_state` row (§6.4).

### 6.3 `bundle_emit_state`

```sql
CREATE TABLE bundle_emit_state (
  asset_rk            text PRIMARY KEY,
  asset_kind          text NOT NULL,
  manifest_sha256     text NOT NULL,
  emitted_at          timestamptz NOT NULL,
  emitter_version     text NOT NULL,
  last_inputs_hash    jsonb NOT NULL                  -- per-source-table sha hashes used in this emission
);
```

`last_inputs_hash` records the hashes of every upstream row used in the emission. The worker may skip emission if the current inputs hash matches the recorded one (no-op).

### 6.4 Catalog index emission

`bundle_catalog` task scans the catalog's schemas and the assets within them, builds `catalog_assets_index.json`, atomically writes, updates a `catalog_emit_state` row analogous to §6.3.

---

## 7. Semantic bindings persistence

Bindings are stored in Postgres for fast resolve and machine consumption; the bundle's `semantic_bindings.json` is a projection.

```sql
CREATE TABLE semantic_bindings (
  asset_rk                 text PRIMARY KEY,
  asset_kind               text NOT NULL,
  primary_object_type      text,
  extracted_at             timestamptz NOT NULL,
  extraction_provenance    text NOT NULL,
  human_reviewed           boolean NOT NULL DEFAULT false,
  human_reviewer           text,
  human_reviewed_at        timestamptz,
  propagated_markings      text[] NOT NULL DEFAULT '{}',
  updated_at               timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE binding_field (
  binding_id        bigserial PRIMARY KEY,
  asset_rk          text NOT NULL REFERENCES semantic_bindings(asset_rk) ON DELETE CASCADE,
  asset_field       text NOT NULL,
  asset_field_rk    text,
  card_field        text NOT NULL,
  binding_kind      text NOT NULL,
  refs_object_type  text,
  card_id           text NOT NULL,
  card_version_seen integer NOT NULL,
  UNIQUE (asset_rk, asset_field)
);

CREATE TABLE binding_interface (
  asset_rk          text NOT NULL,
  interface_id      text NOT NULL,
  via_object_type   text NOT NULL,
  PRIMARY KEY (asset_rk, interface_id)
);

CREATE TABLE binding_causal_participation (
  asset_rk          text NOT NULL,
  causal_node_id    text NOT NULL,
  role              text NOT NULL,                    -- 'subject'|'outcome'|'mediator'
  PRIMARY KEY (asset_rk, causal_node_id, role)
);

CREATE TABLE binding_drift_flag (
  flag_id           bigserial PRIMARY KEY,
  asset_rk          text NOT NULL,
  kind              text NOT NULL,                    -- 'card_version_drift'|'field_missing_in_asset'|'unbound_field_added'
  card_id           text,
  seen_version      integer,
  current_version   integer,
  first_observed_at timestamptz NOT NULL DEFAULT now(),
  resolved_at       timestamptz
);
```

### 7.1 Binding extraction pipeline

On a card edit affecting bound assets, the foundry runs a binding-validator task:

1. For each asset bound to the card, parse the new card body via `ontology_foundry.llm.llm_structured_transform` into a structured bindings draft.
2. Diff the draft against the stored `binding_field` rows.
3. If identical: update `card_version_seen` on the binding rows; clear any `card_version_drift` flag; done.
4. If different and `human_reviewed=true` for this binding: write a `card_version_drift` flag, leave bindings unchanged, surface for human review.
5. If different and binding was never human-reviewed: apply the new bindings; bump `card_version_seen`; mark as machine-updated (no human review claim).

---

## 8. Audit log

Single table for all hierarchy tiers, plus a parallel append-only model for the foundry's claims/relations (already in foundry; not duplicated here).

```sql
CREATE TABLE hierarchy_audit (
  audit_id        bigserial PRIMARY KEY,
  occurred_at     timestamptz NOT NULL DEFAULT now(),
  actor           text NOT NULL,
  action          text NOT NULL,                       -- 'create'|'update'|'append'|'deactivate'|'emit'
  tier            text NOT NULL,                       -- 'T0'|'T1'|'T2'|'T3'|'T4'|'T5'|'T6'|'semantic_layer'|'lineage'|'binding'
  entity_uid      text NOT NULL,
  field_path      text,                                -- e.g., 'business_context', 'entities_of_record[42]'
  old_value       jsonb,
  new_value       jsonb,
  comment         text
);

CREATE INDEX idx_audit_entity ON hierarchy_audit (tier, entity_uid, occurred_at DESC);
CREATE INDEX idx_audit_actor ON hierarchy_audit (actor, occurred_at DESC);
```

Every mutation through `HierarchyStore` writes at least one audit row. Emit operations (`action='emit'`) are also logged for traceability when bundle drift needs to be diagnosed.

---

## 9. Soft-delete policy

| Tier | Removal mode |
|---|---|
| T0 Organization | Soft (`lifecycle_stage = removed`); descendants cascade-tagged but not deleted |
| T1 Source | Soft |
| T2 Catalog | Soft |
| T3 Schema (`schema_metadata`) | Hard at the amundsenrds layer (cascade-delete sidecars); our `schema_ext` cascades |
| T4 Tabular asset (`table_metadata`) | Hard at amundsenrds (cascade) |
| T4 API endpoint | Soft via `is_deprecated`; hard via DELETE only on explicit operator action |
| T4 Function / Metric | Soft via `is_deprecated` |
| T6 Code list / value | Hard (cascade from parent field) |
| Child rows (operating regions, entity claims, lineage edges) | Soft (`active = false`) |

The rule: **top-level user-facing entities are soft-deleted**; intermediate FK chains can hard-delete because their soft-delete is meaningless if their parent went away.

When amundsenrds upstream cascades a delete (e.g., schema disappears in the source), our `lineage_edge` rows pointing into that schema's assets become orphan-targets. The orphan-cleanup worker (§14) flags them with `active=false, lineage_orphan_cleanup` audit.

---

## 10. databuilder integration

We import `amundsendatabuilder` as a dependency and use its `Extractor` / `Transformer` / `Loader` shape. We **do not** use its Publisher / Neo4j sink — our Loader is terminal.

### 10.1 Pipeline shape

```python
# ontology_foundry/ingestion/pipelines.py (to be added)

from databuilder.task.task import DefaultTask
from databuilder.job.job import DefaultJob
# ... databuilder extractors per source kind

def build_snowflake_ingest_job(*, source_id: str, ...) -> DefaultJob:
    extractor = SnowflakeMetadataExtractor(...)
    transformers = [
        SourceContextTransformer(source_id=source_id),
        EntityBindingHintTransformer(...),       # attaches CDM hints if config has them
    ]
    loaders = [
        OntologyFoundryRDSLoader(...),           # writes amundsenrds + sidecars in one txn
        ReindexEnqueueLoader(...),               # post-commit enqueues Qdrant + bundle tasks
    ]
    task = DefaultTask(extractor=extractor, transformers=transformers, loaders=loaders)
    return DefaultJob(conf=conf, task=task)
```

### 10.2 Built-in extractors (databuilder ships)

For these, we use databuilder's extractors as-is:

| Source kind | databuilder extractor |
|---|---|
| Snowflake | `SnowflakeMetadataExtractor` |
| Postgres | `PostgresMetadataExtractor` |
| MySQL | `MysqlMetadataExtractor` |
| Redshift | `RedshiftMetadataExtractor` |
| BigQuery | `BigQueryMetadataExtractor` |
| Databricks (Hive) | `HiveTableMetadataExtractor` (with adaptation) |

These emit amundsenrds records — `TableMetadata`, `ColumnMetadata`, `TableColumnUsage`, etc.

### 10.3 New extractors we author (API + semantic)

| Source kind | New extractor | Emits |
|---|---|---|
| Salesforce | `SalesforceSObjectExtractor` | `APIEndpointMetadata`, `APIFieldMetadata`, `CodeListMetadata`, `CodeValueMetadata` |
| ServiceNow | `ServiceNowDictionaryExtractor` | Same shape |
| Workday | `WorkdayMetadataExtractor` (WSDL/REST) | Same shape |
| Generic OpenAPI | `OpenAPISpecExtractor` | Same shape |
| GraphQL | `GraphQLSchemaExtractor` | Same shape |
| dbt semantic layer | `DbtSemanticLayerExtractor` | `MetricMetadata`, `MetricDimensionMetadata` |
| dbt project | `DbtProjectExtractor` | `view_definition` blocks for dbt models; lineage edges (depends_on) |
| Cube | `CubeSchemaExtractor` | `MetricMetadata` |
| LookML | `LookMLExtractor` | `MetricMetadata` (where dashboards expose them) |

All conform to databuilder's `Extractor.extract()` returning records; we define our record types in `ontology_foundry.ingestion.records`.

### 10.4 Transformers

| Transformer | Purpose |
|---|---|
| `SourceContextTransformer` | Stamps `source_id` (T1 slug) on every record (extractors don't know our T1 layer). |
| `EntityBindingHintTransformer` | If a tenant config maps a table → CDM entity (legacy schema_mapping per existing foundry config), attach a binding hint that downstream emits a draft `semantic_bindings` row. |
| `RkBuilderTransformer` | For records that come without `rk` (some custom extractors), build the `rk` deterministically from identity components. |
| `MarkingPropagationTransformer` | If a source-level marking (e.g., `restricted` sensitivity) needs to push down to child rows, do it here at ingest time rather than at resolve time. |

### 10.5 Loaders

| Loader | Writes to |
|---|---|
| `OntologyFoundryRDSLoader` | amundsenrds tables (`schema_metadata`, `table_metadata`, `column_metadata`, descriptions, badges, owners, usage) + our extension tables (`schema_ext`, `table_ext`, `column_ext`) when ingest carries those — typically `schema_ext`/`table_ext`/`column_ext` are NOT set at first ingest; they're filled in later by authors |
| `ApiAssetLoader` | `api_endpoint_metadata`, `api_field_metadata`, descriptions, code lists/values |
| `FunctionLoader` | `function_metadata`, `function_parameter`, descriptions |
| `MetricLoader` | `metric_metadata`, `metric_dimension` |
| `LineageEdgeLoader` | `lineage_edge` from view/metric/function `depends_on[]` and observed pipeline DAGs |
| `ReindexEnqueueLoader` | Terminal — writes `reindex_queue` rows for every `rk` touched by this job; commits post-Postgres-commit |

All loaders run inside one Postgres transaction per record batch. Either the whole batch lands or none does.

### 10.6 Idempotency

Each loader writes via `INSERT ... ON CONFLICT (...) DO UPDATE SET ...` (Postgres UPSERT). Re-running an ingest job is safe: rows are upserted, descriptions are merged by source, and audit captures only the actual changes.

### 10.7 Scheduling

Per source, an ingest job runs at the source's `refresh_cadence`:
- `streaming`: a continuous worker reads change events (vendor-specific).
- `batch`: cron-scheduled invocations using the configured frequency.
- `snapshot`: explicit operator-invoked runs.

Bootstrap and reconciliation modes are described in §13.

---

## 11. Card persistence pipeline

Cards are filesystem-resident (`semantic_layer_card_spec.md` §2.1). The mirror to Postgres + Qdrant is handled by a `card_sync` worker watching the filesystem.

### 11.1 Authoring path

1. Human edits `tenants/<org_id>/semantic_layer/<kind>s/<id>.card.md`.
2. CI on the PR runs `ontology_foundry.eval.gates` against the file:
   - `gate_id_pattern`, `gate_nonempty_body`, `gate_refs_resolve`.
   - The new gates from `semantic_layer_card_spec.md` §10.1.
3. On gate pass, PR merges. A `card_sync` worker picks up the change.
4. Worker parses frontmatter + body; computes `body_hash`.
5. Worker writes to `card` table (new version row, `valid_to = now()` on the prior row).
6. Worker rebuilds `card_ref` rows for this card.
7. Worker enqueues `qdrant_card` for re-embed.
8. Worker enqueues binding-validator tasks for every asset referencing this card (per §7.1).
9. Audit row written with `tier='semantic_layer'`.

### 11.2 LLM-assisted authoring (derived cards)

The foundry's extraction passes propose cards under `_derived/`. They reach Postgres only after a human merges them out of `_derived/`, via the same authoring path.

### 11.3 Pack card onboarding

Pack distributions ship a versioned card directory. `pack_sync` worker mounts the pack tree read-only into Postgres with `origin='pack'`. Pack version is recorded in `tenants/<org_id>/pack_pinning.yaml`. Pack updates are pinned per-tenant.

---

## 12. Migration heads

### 12.1 Two heads

```
[amundsenrds_alembic_head]   <-- shipped by amundsenrds; we don't author
       ↑ depended_on_by
[ours_alembic_root]
       ↑
[ours_alembic_001_add_organization]
       ↑
[ours_alembic_002_add_source]
       ↑
...
```

`ours_alembic_root` has `down_revision = <amundsenrds_latest>` (the upstream head at our pinned commit). Our chain is a superset.

### 12.2 Bootstrap order

1. Run `alembic upgrade <amundsenrds_head>` against the database.
2. Run `alembic upgrade <ours_head>` against the database.
3. Verify schema (a smoke test that checks expected tables exist).

### 12.3 Upgrading amundsenrds pin

1. Re-pin amundsenrds to a newer commit.
2. Verify that new amundsenrds migrations are additive (no breaking renames to fields we depend on in views; if there are, file an issue against the pin choice).
3. `alembic upgrade <new_amundsenrds_head>`.
4. Re-run `alembic upgrade <ours_head>` to apply any of our migrations whose dependencies shifted.

### 12.4 What we never do

- Modify amundsenrds tables in our migrations.
- Insert columns into amundsenrds tables via `ALTER`.
- Override amundsenrds Alembic migrations.

All extension fields go in sidecars. This keeps the upstream upgrade path clean.

---

## 13. Bootstrap and reconciliation

### 13.1 Bootstrap (new source)

1. Operator writes `source.yaml` (T0/T1 spec) and registers it via `create_source()`.
2. Operator configures connector credentials.
3. First ingest run: full extraction. May be slow; subsequent runs are incremental.
4. Bundle emission cascades — every asset emitted.
5. Card binding draft pass — for each asset, propose a `semantic_bindings.json` draft. Mark `human_reviewed=false`. Surface to authoring queue.

### 13.2 Reconciliation (existing source)

A nightly reconciler:
1. For each source, query the source's current schema list. Diff against `schema_metadata`.
2. New schemas → enqueue `bundle_asset` for each new asset.
3. Missing schemas → mark `lifecycle_stage = removed` on `schema_ext` rows; cascade to assets; flag binding drift.
4. Repeat for each asset within schemas (column changes, rename detection).

### 13.3 Manual reconcile

`HierarchyStore.reconcile(*, source_id)` triggers a same-shape pass on demand. Used by operators after large vendor-side changes.

### 13.4 Drift surfaces

| Drift kind | Where surfaced |
|---|---|
| Schema missing in source but present in our DB | `lifecycle_stage='removed'` + audit |
| New column observed but not bound | `binding_drift_flag.kind='unbound_field_added'` |
| Bound column no longer in source | `binding_drift_flag.kind='field_missing_in_asset'` |
| Card edit but bindings haven't been re-extracted | `binding_drift_flag.kind='card_version_drift'` |
| Owner field empty for a `lifecycle_stage='production'` asset | Governance dashboard signal (not a flag table; periodically computed) |

---

## 14. Workers and operational cadences

| Worker | Cadence | Responsibility |
|---|---|---|
| `ingest-<source_id>` | Per source `refresh_cadence` | Pull vendor metadata via databuilder pipeline |
| `reindex-worker` | Continuous (long-poll `reindex_queue`) | Process Qdrant reindex + bundle emit + lineage derive tasks |
| `card-sync` | Continuous (filesystem watch) | Mirror card filesystem changes into Postgres + enqueue downstream tasks |
| `binding-validator` | Triggered by card edits | Re-extract bindings for affected assets |
| `reconciler` | Nightly | Full reconcile pass; drift surfacing |
| `orphan-cleanup` | Nightly | Mark `lineage_edge` rows pointing to vanished rks |
| `audit-pruner` | Monthly | Archive `hierarchy_audit` rows older than retention horizon (default 2 years) to cold storage |

All workers are stateless; their state lives in queues + Postgres. Horizontal scaling is by partitioning the work key space (rk-hash).

---

## 15. Error handling

### 15.1 Postgres write failures

Transactional. Either commit or rollback. The caller sees the error.

### 15.2 Qdrant failures

Logged on the `reindex_queue` row; retried with exponential backoff. After 5 attempts, the row goes `failed` and is surfaced to the ops dashboard. Search results may be stale; SoR remains correct.

### 15.3 Bundle emission failures

Same pattern as Qdrant. Bundle is stale; storage is correct. Consumers reading bundles can fall back to querying storage directly via the `BundleStore` (consumer spec).

### 15.4 Card validation failures

Block the merge at CI time. No half-validated cards reach Postgres.

### 15.5 Ingest pipeline failures

databuilder's tasks fail loudly. The pipeline is retried at the next scheduled run. Partial extractions are not committed (per §10.5 batch transactionality).

---

## 16. Observability

Every worker emits structured logs with:
- `correlation_id` (from the originating mutation or ingest run)
- `tier`, `entity_uid`
- `task_kind`, `duration_ms`, `outcome`

Metrics exposed:
- `hierarchy_writes_total{tier, action}`
- `reindex_queue_depth{task_kind, status}`
- `bundle_emit_latency_seconds{asset_kind}` (histogram)
- `ingest_pipeline_duration_seconds{source_id, source_kind}`
- `binding_drift_flags_open{kind}`
- `card_validation_failures_total{gate}`

---

## 17. Bootstrap procedure (one-time, per env)

1. Provision Postgres, Qdrant.
2. `alembic upgrade <amundsenrds_head>`.
3. `alembic upgrade <ours_head>`.
4. Seed platform pack cards (from `card_emitter_design.md` pack distribution).
5. Verify smoke tests:
   - `hier_t0_orgs_<env>` Qdrant collection created.
   - `cards_<tenant_id>` collection created for the first tenant.
   - `reindex-worker`, `card-sync`, `reconciler` services started.
6. Onboard first tenant:
   - Write `organization.yaml`, `source.yaml` files.
   - Run `HierarchyStore.import_yaml(...)` (bulk import helper).
   - Configure ingest job for the source; run first ingest.
7. Verify bundles emitted under `tenants/<org_id>/assets/...`.

---

## 18. Open items

- **Streaming-source extraction model.** Currently batch-shaped. The first streaming customer onboards the streaming worker pattern.
- **Cross-tenant pack-update orchestration.** Pack version is pinned per tenant. Bulk pack upgrades across many tenants are operator-initiated; tooling not yet specced.
- **Bundle archival retention.** How long to retain bundle file generations on disk vs. expire to cold storage. Default: keep on disk forever; revisit at scale.
- **`reindex_queue` partitioning** at high write volumes — current single-table-with-index is fine to a few million pending rows. Beyond that, partition by `task_kind` or move to a real queue.

---

## 19. Change log

| Date | Change |
|---|---|
| 2026-05-15 | Initial draft. |
