# MDL Auto-Generation from Source — Specification

**Status:** Draft 2026-05-16.
**Part of:** Data Knowledge Hierarchy series.
**Depends on:** `mdl_bundle_spec.md`, `T2_to_T6_amundsenrds_sidecar_spec.md`, `mdl_table_concept_annotation_spec.md`, `hierarchy_persistence_and_ingestion_spec.md`.
**Pipeline posture:** **Greenfield, additive.** Wraps existing `genieml/dataservices/` components into an end-to-end auto-build pipeline; does not replace them.
**Worked example throughout:** the ServiceNow / Cornerstone OnDemand dump at `/Users/sameerm/Downloads/servicenow_backup.sql` — 241 tables, 1,893 column comments, 0 table comments, `pg_dump -Fc` v1.16 binary format.

---

## 1. Scope

This spec defines a pipeline that takes a data source (live connection or offline DDL dump) and produces:

1. **MDL JSON v2 documents** — one per asset, populated with `name`, `rk`, descriptions, columns, materialization, view definitions, and FK references.
2. **Bottoms-up annotations** — `concepts[]`, `key_areas[]`, `causal_relations[]` per asset (handed off to the annotation enricher from `mdl_table_concept_annotation_spec.md`).
3. **Lineage edges** — `lineage_edge` rows for declared FKs and view dependencies.
4. **Code lists** — T6 `code_list` + `code_value` rows for enum-shaped columns and accompanying lookup tables.
5. **amundsenrds rows + sidecar rows** populated end-to-end.

**Scope = the pipeline itself.** The annotation step is the prior spec; the storage shapes are the T2–T6 spec; the bundle emission is `mdl_bundle_spec`. This spec is the orchestration glue plus the **MDL generation from raw DDL** step that didn't have a home before.

### 1.1 v1 source kinds

| Kind | Path |
|---|---|
| Postgres (live) | `ERDExtractor._extract_postgresql_schema` (exists) |
| Snowflake (live) | `ERDExtractor._extract_snowflake_schema` (exists) |
| Salesforce | New `SalesforceSObjectExtractor` (per `hierarchy_persistence_and_ingestion_spec.md` §10.3) |
| ServiceNow | New `ServiceNowDictionaryExtractor` |
| **Plain-SQL DDL dump** (`pg_dump -F p`) | New `PlainSqlDdlExtractor` |

Out of v1: BigQuery, MySQL, Oracle, MongoDB are introspectable via existing `ERDExtractor` methods but not in the v1 orchestration suite; add as second-wave.

### 1.2 Out of scope

- **`pg_dump -Fc` (custom format)** — the ServiceNow dump is in this binary format. Local `pg_restore` 17.5 cannot read v1.16. We require the operator to re-export as plain SQL (`pg_dump -F p ...`) to use the offline path, or to restore the dump into a live Postgres and use the live path.
- BI tool extraction (Looker / Tableau dashboards) — handled by a separate spec.
- dbt project ingestion — the existing `cubes/` flow + new dbt extractor; tangential to this spec.
- Incremental / CDC ingestion — handled by `hierarchy_persistence_and_ingestion_spec.md` §13 reconciliation.

---

## 2. Existing components to leverage (no rewrite)

| Component | Where | What it gives us |
|---|---|---|
| `ERDExtractor` | `app/service/ERDextraction_service.py` | Native introspection per DB kind; returns `(tables, columns, types, comments, PKs, FKs, indexes)` for PG / Snowflake / others. |
| `LLMSchemaDocumentationGenerator` | `app/agents/schema_manager.py` | LLM-generated table + column descriptions; produces MDL JSON. `process_and_store_schema` is the existing end-to-end. |
| `RelationshipRecommendation` | `app/agents/relationship_recommendation.py` | LLM-suggested FK/relationships when DDL lacks declared FKs. |
| `SemanticsDescription` | `app/agents/semantics_description.py` | Per-column semantic description (units, semantic_unit candidates). |
| `DomainWorkflowService.{add_table, commit_workflow}` | `app/service/project_workflow_service.py` | Orchestration shape, transactional commit. |
| `DataRetriever._get_*_data` | `app/service/datasource_service.py` | Live sample rows for LLM context. |
| `enrich_table_metadata` | `app/agents/cubes/metadata_enrichment.py` | Column statistics from a DataFrame. |
| `KnowledgeBaseService` / `InstructionService` | `app/service/` | Persist instructions and KB entries derived from MDL. |

The new pipeline imports these and adds **three** new components:

1. `PlainSqlDdlExtractor` — for offline `pg_dump -F p` DDL files (the alternative to live connection).
2. `AutoMDLBuildOrchestrator` — orchestrates introspect → document → annotate → store → emit.
3. Integration adapters that route the existing services' outputs into amundsenrds + sidecar rows.

---

## 3. End-to-end pipeline shape

```
[Input: live connection OR plain-SQL DDL file]
    │
    ▼
Step A — Source Introspection
   • Live: ERDExtractor._extract_<kind>_schema(connection)
   • Offline (plain SQL): PlainSqlDdlExtractor.parse(file_path)
   • Output: SchemaIntrospectionResult
       { tables[]: { name, schema, columns[], pk[], fks[], indexes[], 
                     description?, column_comments{name->desc} }, 
         views[]: { name, schema, columns[], view_definition, depends_on?[] },
         routines[]: { name, signature, return_type, language, body? }
       }
    │
    ▼
Step B — Sample Data Collection (optional, live only)
   • DataRetriever.get_data_from_connection(conn, tables, row_limit=50)
   • Output: SampleRows { table_name -> [row, row, ...] }
   • Skipped for offline DDL or when sampling fails (network, perms)
    │
    ▼
Step C — MDL Generation
   • C.1 Deterministic mapping: table/column DDL → MDL skeleton
   • C.2 Preserve native COMMENTs as descriptions (provenance: extractor:<kind>_information_schema)
   • C.3 LLMSchemaDocumentationGenerator fills GAPS only:
         - Table description when none exists
         - Column descriptions when COMMENT is missing
         - Semantic units / PII flags via SemanticsDescription
   • C.4 RelationshipRecommendation runs ONLY for tables with zero declared FKs
   • C.5 Code list detection: low-cardinality columns + *_enum_* lookup tables → T6
   • Output: GeneratedMDLBundle
       { mdl_json: { models[], endpoints[], functions[], metrics[] }, 
         lineage_edges[]: { from_rk, to_rk, edge_kind, evidence_kind }, 
         code_lists[]: { rk, parent_rk, values[] } }
    │
    ▼
Step D — Annotation Enrichment
   • Delegates to AssetAnnotationEnricher per mdl_table_concept_annotation_spec.md §5
   • LLM proposes concepts[], key_areas[], causal_relations[]
   • Auto-applies with provenance llm_enrichment, no-clobber on existing service/human edits
    │
    ▼
Step E — Storage Write-Through
   • Writes amundsenrds rows (database_metadata, cluster_metadata, schema_metadata,
     table_metadata, column_metadata, *_description, *_programmatic_description)
   • Writes sidecar rows (table_ext, column_ext, schema_ext, ...)
   • Writes lineage_edge, code_list, code_value
   • Writes asset_annotation_provenance
   • All transactional per the persistence/ingestion spec
    │
    ▼
Step F — Reindex + Bundle Emit (enqueued, async)
   • Qdrant indexing per hier_t* collections
   • Bundle emission per asset (mdl.json + sidecars)
   • Async; pipeline returns to caller after Step E commits
```

The orchestrator is the new code; Steps A and parts of C are existing dataservices code; Steps D, E, F are specced elsewhere.

---

## 4. Source connector contract

```python
class SourceIntrospector(Protocol):
    kind: str                              # 'postgres' | 'snowflake' | 'salesforce' | 'servicenow' | 'plain_sql_ddl'

    def introspect(self, *, connection_or_path: str | dict,
                   schemas: list[str] | None = None,
                   include_views: bool = True,
                   include_routines: bool = True) -> SchemaIntrospectionResult: ...

    def sample(self, *, connection: dict, tables: list[str],
               row_limit: int = 50) -> SampleRows | None: ...
    # Returns None when sampling isn't possible (offline mode, perms denied)
```

Implementations:

| Implementation | Strategy |
|---|---|
| `PostgresIntrospector` | Delegates to `ERDExtractor._extract_postgresql_schema` |
| `SnowflakeIntrospector` | Delegates to `ERDExtractor._extract_snowflake_schema` |
| `SalesforceIntrospector` | New; reads `/services/data/v59.0/sobjects` describe |
| `ServiceNowIntrospector` | New; reads `sys_dictionary` + `sys_db_object` |
| `PlainSqlDdlIntrospector` | New; parses plain SQL DDL (§7) |

### 4.1 `SchemaIntrospectionResult` shape

```python
@dataclass
class SchemaIntrospectionResult:
    source_kind: str
    catalog: str | None                          # database name; may be None for some sources
    schemas: list[SchemaInfo]
    extracted_at: datetime

@dataclass
class SchemaInfo:
    name: str
    description: str | None                      # rarely populated by sources
    tables: list[TableInfo]
    views: list[ViewInfo]
    routines: list[RoutineInfo]

@dataclass
class TableInfo:
    name: str
    description: str | None                      # COMMENT ON TABLE value, if present
    columns: list[ColumnInfo]
    primary_key: list[str]                       # column names
    foreign_keys: list[ForeignKey]
    indexes: list[Index]
    row_count_estimate: int | None

@dataclass
class ColumnInfo:
    name: str
    sql_type: str                                # raw DDL type, e.g. 'VARCHAR(220)'
    nullable: bool
    default: str | None
    description: str | None                      # COMMENT ON COLUMN value, if present
    enum_candidates: list[str] | None            # populated when low-cardinality + finite domain detected
```

---

## 5. Step C — MDL Generation in detail

The deterministic part is most of the work. LLM is called only where source data is genuinely missing.

### 5.1 Deterministic mappings

| From | To MDL field |
|---|---|
| `TableInfo.name` | `models[].name` |
| `(source_id, catalog, schema_name, table_name)` | `models[].rk` (per the rk convention in T2–T6 spec §3) |
| `ColumnInfo.name` | `models[].columns[].name` |
| `ColumnInfo.sql_type` | `models[].columns[].type` |
| `ColumnInfo.nullable=false` | `models[].columns[].notNull=true` |
| `ColumnInfo.description` (when non-null) | `models[].columns[].properties.description` + `description_provenance: "extractor:<kind>_information_schema"` |
| `TableInfo.description` (when non-null) | `models[].description` + `description_provenance: "extractor:<kind>_information_schema"` |
| `TableInfo.primary_key` | `columns[].properties.is_primary_key=true` for those names |
| `TableInfo.foreign_keys` | `lineage_edge` rows with `edge_kind: depends_on`, `evidence_kind: declared_fk`, plus `columns[].properties.references` hint |
| `ViewInfo.view_definition` | `models[].view_definition.query`, with `is_view=true` |
| `ViewInfo.depends_on` (parsed from query) | `view_definition.depends_on[]` + `lineage_edge` rows |
| `RoutineInfo` | `functions[]` entry |

### 5.2 LLM-assisted gaps

The deterministic step produces a "near-MDL" with two kinds of gaps:

1. **Missing table descriptions** — for the ServiceNow dump: 241 of 241 tables. LLM generates from `(name, columns[], column_descriptions[], schema_context)`.
2. **Missing column descriptions** — for the ServiceNow dump: 241 × ~10 columns ≈ 2,500 columns; 1,893 have native comments, leaving ~600 gaps. LLM fills the gaps.

Batched: each LLM call carries 1 table's full structure (columns ≤ 50) and returns table description + missing column descriptions in one structured response.

```python
class TableDocumentationLLM:
    async def document_gaps(self, *,
                            table: TableInfo,
                            schema_context: SchemaInfo,
                            sample_rows: list[dict] | None,
                            candidate_cards: list[CardSummary]    # for grounding
                           ) -> DocumentationFill: ...
```

`LLMSchemaDocumentationGenerator.document_table_schema` is repurposed for this step — it already does roughly this; the change is making it *gap-aware* (skip columns that already have native COMMENT) and removing the "always rewrite" behavior.

### 5.3 FK detection — declared vs inferred

- **Declared FKs** (from `ALTER TABLE ... FOREIGN KEY`): land in `lineage_edge` with `evidence_kind='declared_fk'`, `confidence=1.0`.
- **Inferred FKs** (no DDL FK but column naming suggests one, e.g. `employee_id` in many tables): handled by `RelationshipRecommendation` only when the table has zero declared FKs. `evidence_kind='inferred_relationship'`, `confidence` per the recommender's score. Treated as candidates; downstream services may promote to declared.

For the ServiceNow dump specifically, the dump's `ALTER TABLE` clauses carry the FK constraints, so most relationships are declared, not inferred. Recommender runs as a safety net.

### 5.4 Code list detection — when a column becomes T6

Two signals combine:

1. **Naming heuristic**: column name ends in `_id` and references a table named `*_enum_*` or `*_code` or `*_lookup`.
2. **Cardinality + value-set check** (live mode only): sample the column; if distinct count < 50 AND domain is stable across samples, it's a code list candidate.

Detected code lists generate `code_list` + `code_value` rows. Column `column_ext.code_list_rk` is populated. MDL `columns[].code_list_rk` carries the reference.

For the ServiceNow dump: the `application_cf_enum_local2_core` table shape (`option_id`, `option_value`, `property_id`, `culture_id`) is the dump's code-list pattern — detected by naming + structure. ~10–20 such tables expected.

---

## 6. Live vs offline modes

| Capability | Live (Postgres / Snowflake) | Offline (Plain SQL DDL) |
|---|---|---|
| DDL extraction | Via `information_schema` and `pg_catalog` | Parsed from text |
| Native COMMENTs | Yes | Yes (via `COMMENT ON ...` statements) |
| Sample rows for LLM context | Yes, via `DataRetriever` | No |
| Cardinality / row counts | Estimated from `pg_class.reltuples` etc. | Unavailable |
| Enum / code-list detection (value-set) | Yes (sample) | Falls back to naming heuristic only |
| FK consistency check (does referenced table exist?) | Yes | Yes (full DDL is parsed) |
| Idempotent re-runs | Yes, content-hash-keyed | Yes |
| Cost | Network + sample read | Zero external; just CPU |

Offline mode degrades sample-based enrichments but produces a complete, valid MDL skeleton. For ServiceNow specifically the dump path is fine.

---

## 7. `PlainSqlDdlExtractor` — offline DDL parsing

### 7.1 What it accepts

- `pg_dump -F p` plain SQL files (Postgres flavored, ANSI-ish).
- Future: `mysqldump`, Snowflake `GET_DDL` exports, generic ANSI DDL.

Does **not** accept:
- `pg_dump -Fc` custom format binary (the ServiceNow dump is this; operator must re-export, or restore + use live mode).
- Compressed dumps (operator decompresses first).
- Schema dumps that mix INSERT data with DDL — we parse only the DDL portion.

### 7.2 Parser strategy

Two-pass parse:

1. **Lex**: split into statements by `;` terminator, respect quoting + dollar-quoting (Postgres functions).
2. **Per-statement parse**: classify into `CREATE TABLE`, `CREATE VIEW`, `CREATE FUNCTION`, `ALTER TABLE ... ADD CONSTRAINT`, `COMMENT ON {TABLE|COLUMN|VIEW} ...`, `CREATE INDEX`, ignore everything else.

Parser library: `sqlglot` (already a common dependency) with `dialect='postgres'`. Falls back to regex for `COMMENT ON` (sqlglot's coverage of these is incomplete for some dialects).

Implementation: `app/service/plain_sql_ddl_extractor.py` (new file in dataservices).

### 7.3 Output

`SchemaIntrospectionResult` shaped identically to the live introspectors. The orchestrator does not branch on mode.

### 7.4 What we don't try to handle

- Cross-schema FKs to schemas not in the file (silently dropped; warned).
- Stored procedure bodies (parsed only for signature + return type).
- Trigger / rule / policy definitions (skipped).
- Type system extensions (`CREATE TYPE`, `CREATE DOMAIN`) — minimal support: types become opaque strings.

---

## 8. `AutoMDLBuildOrchestrator`

The new orchestrator. Composes the existing services.

```python
class AutoMDLBuildOrchestrator:
    def __init__(self, *,
                 source_introspectors: dict[str, SourceIntrospector],
                 documentation_llm: LLMSchemaDocumentationGenerator,
                 relationship_llm: RelationshipRecommendation,
                 semantics_llm: SemanticsDescription,
                 annotation_enricher: AssetAnnotationEnricher,
                 store: HierarchyStore,
                 sampler: DatasetSampleRetriever | None = None):
        ...

    async def build_from_live(self, *,
                              source_id: str,
                              connection_id: str,
                              schemas: list[str] | None = None,
                              sample: bool = True,
                              actor: str = "system") -> AutoBuildResult: ...

    async def build_from_dump(self, *,
                              source_id: str,
                              file_path: str,
                              dialect: str = "postgres",
                              actor: str = "system") -> AutoBuildResult: ...

@dataclass
class AutoBuildResult:
    source_id: str
    tables_ingested: int
    views_ingested: int
    routines_ingested: int
    native_column_comments_preserved: int
    column_descriptions_generated_by_llm: int
    table_descriptions_generated_by_llm: int
    fks_declared: int
    fks_inferred: int
    code_lists_detected: int
    annotations_concepts_applied: int
    annotations_key_areas_applied: int
    annotations_causal_relations_applied: int
    llm_calls_total: int
    llm_tokens_in: int
    llm_tokens_out: int
    wall_time_seconds: float
    warnings: list[str]
```

The result object is what telemetry + ops dashboards consume.

### 8.1 Orchestration sequence per asset

For each table in the introspection result:

```
1. Compute rk from (source_id, catalog, schema, table_name)
2. Check if already exists in storage:
     a. If yes AND inputs_hash(table) matches prior — skip (idempotent no-op)
     b. If yes AND content changed — proceed; existing annotations honor no-clobber rule
3. Build MDL skeleton from TableInfo (deterministic)
4. Identify description gaps; if any, call documentation_llm.document_gaps()
5. For columns with no semantic_unit hint: optionally call semantics_llm
6. If no declared FKs and table participates in joins: call relationship_llm
7. Begin Postgres transaction
8. Upsert amundsenrds rows (database, cluster, schema, table, columns, descriptions)
9. Upsert sidecar rows (table_ext, column_ext)
10. Upsert lineage_edge rows
11. Upsert code_list + code_value rows
12. Commit
13. Enqueue annotation enrichment task (Step D — async)
14. Enqueue Qdrant + bundle emission (Step F — async)
```

Steps 8–12 are the integration adapters mentioned in §2.

### 8.2 Concurrency

The orchestrator processes tables in parallel up to `max_concurrency` (default 8). LLM calls per table are independent; storage writes serialize via a single connection-pool transaction batcher.

For a 241-table source: with `max_concurrency=8` and ~3-second LLM calls + ~50ms storage, total wall time is roughly `241 / 8 × 3.5s ≈ 105 seconds` plus the annotation step which queues async.

### 8.3 Failure handling

- **Per-table failures** isolate: one table's parse / LLM / write failure doesn't abort the orchestrator. The failure is recorded in `warnings[]`; the run continues.
- **Source-wide failures** (connection lost, dump file unreadable) abort the run with a structured error.
- **Resume**: the orchestrator is idempotent — `build_from_live` re-invoked after a partial failure picks up where it left off via the content-hash skip in step 2.

---

## 9. Integration with `DomainWorkflowService`

The existing `DomainWorkflowService` is the operator-facing workflow API. The orchestrator integrates as:

```python
class DomainWorkflowService:
    async def auto_build_from_source(self, *,
                                     source_id: str,
                                     mode: Literal['live', 'dump'],
                                     connection_id: str | None = None,
                                     dump_file_path: str | None = None,
                                     schemas: list[str] | None = None) -> AutoBuildResult:
        # 1. Validate inputs
        # 2. Create or reuse domain (legacy concept; maps to T0 Organization scope)
        # 3. Call AutoMDLBuildOrchestrator.build_from_{live, dump}
        # 4. Optionally trigger commit_workflow if the existing domain commit pattern applies
        # 5. Return AutoBuildResult plus a domain reference for the legacy UI
        ...
```

The existing `add_table` / `commit_workflow` per-table workflow stays available for operators who want hand-curated tables. The auto-build is the bulk-onboarding path; per-table workflow is the refinement path. Operators choose.

---

## 10. Worked example — running this on the ServiceNow dump

The dump is `pg_dump -Fc` binary, not currently parseable by the offline path. Two routes:

### Route A — Restore the dump, run live

```bash
createdb servicenow_local
pg_restore -d servicenow_local /Users/sameerm/Downloads/servicenow_backup.sql
```

Then via `DomainWorkflowService`:

```python
result = await domain_workflow.auto_build_from_source(
    source_id="csod-servicenow-local",
    mode="live",
    connection_id="<connection registered against servicenow_local>",
    schemas=["public"],
)
```

### Route B — Re-export as plain SQL, run offline

```bash
# Requires a Postgres that can read the dump first
pg_restore -f /tmp/servicenow.sql /Users/sameerm/Downloads/servicenow_backup.sql
# Now /tmp/servicenow.sql is plain SQL
```

```python
result = await domain_workflow.auto_build_from_source(
    source_id="csod-servicenow-offline",
    mode="dump",
    dump_file_path="/tmp/servicenow.sql",
)
```

### Expected outcome

| Metric | Expected |
|---|---|
| `tables_ingested` | 241 |
| `native_column_comments_preserved` | 1,893 |
| `column_descriptions_generated_by_llm` | ≤ 700 (the gap after native comments) |
| `table_descriptions_generated_by_llm` | 241 (all tables need it) |
| `fks_declared` | depends on dump — likely 100–300 |
| `fks_inferred` | small, only for tables without declared FKs |
| `code_lists_detected` | 10–25 (the `*_enum_*_core` family) |
| `annotations_concepts_applied` | ≥ 200 (most tables) |
| `annotations_key_areas_applied` | ≥ 220 |
| `annotations_causal_relations_applied` | 50–100 (only tables that feed causal_node cards) |
| `llm_calls_total` | ≤ 500 (description gaps + annotations, batched) |
| `llm_tokens_in / out` | ~600K / 200K |
| `wall_time_seconds` | < 300s with `max_concurrency=8` |

### 10.1 Acceptance criteria for the test fixture

When running against the dump as a fixture in CI:

- **A1**: All 241 tables produce an MDL entry; no orchestrator-level failures.
- **A2**: ≥ 99% of native `COMMENT ON COLUMN` statements appear verbatim in the resulting MDL columns' `description` field with `description_provenance: "extractor:postgres_information_schema"`.
- **A3**: Every `ALTER TABLE ... FOREIGN KEY` constraint produces exactly one `lineage_edge` row with `edge_kind='depends_on'`, `evidence_kind='declared_fk'`.
- **A4**: Annotation gates run as warn (per the annotation spec); no annotation gate produces a hard failure.
- **A5**: `llm_calls_total` ≤ 500.
- **A6**: Re-running the orchestrator against the same dump produces zero writes (content-hash idempotency).

---

## 11. Observability

Per-run telemetry:

```json
{
  "event": "auto_mdl_build_completed",
  "source_id": "csod-servicenow-local",
  "mode": "live",
  "result": { ... AutoBuildResult ... },
  "per_table_summary_log": "..../auto_build_<run_id>.log"
}
```

Aggregations: cost per source, latency per source kind, native-comment preservation rate (should be ~100%), LLM-fill ratio per source.

---

## 12. Operations

### 12.1 First-time onboarding of a new source

```bash
# 1. Register the source via T0/T1 spec
python -m ontology_foundry.cli source.create \
  --source-id csod-servicenow-local \
  --org-id acme-corp \
  --kind postgres \
  --instance-name "ServiceNow Local Mirror"

# 2. Register the connection
python -m ontology_foundry.cli connection.create \
  --source-id csod-servicenow-local \
  --type postgres --host localhost --db servicenow_local

# 3. Build the MDL
python -m ontology_foundry.cli source.auto-build \
  --source-id csod-servicenow-local \
  --mode live \
  --schemas public

# 4. Verify
python -m ontology_foundry.cli source.summary \
  --source-id csod-servicenow-local
```

### 12.2 Updating after upstream schema change

The reconciler (per `hierarchy_persistence_and_ingestion_spec.md` §13) handles incremental change. Manual re-run:

```bash
python -m ontology_foundry.cli source.auto-build \
  --source-id csod-servicenow-local \
  --mode live \
  --schemas public
# Content-hash idempotency: re-runs are cheap when nothing changed.
```

### 12.3 Replacing LLM-generated descriptions with native

If an operator backfills `COMMENT ON TABLE` statements in the source after the initial run, re-running prefers native over LLM. Mechanic: `description_provenance='extractor:...'` always wins over `description_provenance='llm_doc_gap_fill'` at next regenerate.

---

## 13. Open items

- **`pg_dump -Fc` v1.16 support** — would require a newer `pg_restore` or a custom binary parser. Deferred; not worth the effort for v1.
- **Live data sampling toggles** — large tables, sampling row counts vs sampling time. Currently fixed at 50 rows; revisit when first slow-sampling failure occurs.
- **Schema diff narrative** — when an existing source is re-built and the schema has drifted, produce an operator-readable diff (tables added, removed, columns renamed). Useful for change management; defer until first real drift event.
- **Cross-source seed cards** — when onboarding a new source, the LLM annotation step is more effective if relevant pack cards (Employee, Customer, etc.) are already present. Document recommended pack-card pre-installation; not enforced.
- **Salesforce/ServiceNow extractor implementations** — referenced in v1 source kinds but the actual extractor code is in `hierarchy_persistence_and_ingestion_spec.md` §10.3. Cross-track work item.

---

## 14. Cross-spec amendments (deferred)

| Spec | Section | Change |
|---|---|---|
| `hierarchy_persistence_and_ingestion_spec.md` | §10 | Add `AutoMDLBuildOrchestrator` as the wrapper composing databuilder extractors + this pipeline. |
| `mdl_table_concept_annotation_spec.md` | §5 | Note that the auto-build pipeline calls the enricher as Step D. |
| `T0_T1_organization_source_spec.md` | §3 | Mention `auto_build_from_source` as the supported bootstrap operation for new sources. |

Apply when implementation lands.

---

## 15. Change log

| Date | Change |
|---|---|
| 2026-05-16 | Initial draft. Pipeline definition + ServiceNow dump as worked example. |
