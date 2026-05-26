# Storage Topology & Retrieval Reference

**Status:** Reference 2026-05-17. Not a spec — a consolidated navigation page across the 15-spec stack.
**Sources:** All specs in this directory. See §10 for per-section spec cross-references.
**Audience:** Engineers implementing or operating the foundry who need to find where something lives without reading every spec.

---

## 1. What this is

This document consolidates **where data lives**, **how retrieval reaches it**, and **what the outcomes look like for concrete questions**. It does not introduce new design decisions; all content traces to an existing spec.

Three parts:
1. **Storage topology** — every Postgres table, Qdrant collection, and filesystem artifact (§2–§4).
2. **Retrieval mechanism** — the v2 retrieval module shape and how it reaches each store (§5–§6).
3. **Example retrievals** — eight worked examples showing question → retrieval calls → outcomes (§7).

---

## 2. Postgres tables — full inventory

Grouped by purpose. Storage owner: **(A)** = amundsenrds upstream, adopted as-is; **(O)** = ours.

### 2.1 Spine — hierarchy

| Table | Owner | Tier | Purpose |
|---|---|---|---|
| `organization` | O | T0 | Org identity, locale, industry, compliance regimes |
| `operating_region` | O | T0 child | Per-region governance + locale overrides |
| `source` | O | T1 | A logical source instance; FKs `cluster_metadata.rk` for tabular sources |
| `synthetic_cluster` | O | T1 | For API-shaped sources without a real `cluster_metadata` row |
| `entity_authority_claim` | O | T1 child | Append-only human declarations of "this source is authoritative for entity X" |
| `database_metadata` | A | T1 | Platform kind enumeration ("snowflake", "postgres", ...) |
| `cluster_metadata` | A | T1 | An instance of a database (one Snowflake account, one Postgres server) |
| `catalog` | O | T2 | Catalog/database namespace inside a source |
| `schema_catalog` | O | T2↔T3 sidecar | Joins `schema_metadata.rk` to `catalog.catalog_uid` |
| `schema_metadata` | A | T3 | A schema namespace inside a cluster |
| `schema_description` | A | T3 | User-authored schema description |
| `schema_programmatic_description` | A | T3 | Extractor-authored schema description |
| `schema_ext` | O | T3 | Our knowledge wrapper: `purpose`, `domain_tags`, `lifecycle_stage`, sensitivity overrides |

### 2.2 T4 assets — tabular subtype

| Table | Owner | Purpose |
|---|---|---|
| `table_metadata` | A | Tables, views, materialized views |
| `table_description` | A | User-authored |
| `table_programmatic_description` | A | Extractor-authored |
| `table_owner` / `table_follower` / `table_usage` | A | Governance + usage signals |
| `table_ext` | O | Our wrapper: `purpose`, `lifecycle_stage`, `is_materialized`, `view_definition`, `concepts[]`, `key_areas[]`, `causal_relations[]`, `data_product_member[]` |
| `column_metadata` | A | Column definitions, types, FK refs |
| `column_description` | A | User-authored column descriptions |
| `column_programmatic_description` | A | Extractor-authored column descriptions |
| `column_stat` | A | Column statistics (distinct count, nulls, sample values) |
| `column_badge` | A | Column-level badges (PII, etc.) |
| `column_ext` | O | Our wrapper: `purpose`, `is_pii`, `pii_categories`, `is_business_key`, `semantic_unit` |

### 2.3 T4 assets — API endpoint subtype

| Table | Owner | Purpose |
|---|---|---|
| `api_endpoint_metadata` | O | Endpoint identity, HTTP methods, auth scopes, pagination kind |
| `api_endpoint_description` | O | User-authored |
| `api_endpoint_programmatic_description` | O | Extractor-authored |
| `api_endpoint_owner` / `api_endpoint_follower` / `api_endpoint_usage` | O | Parallel to amundsenrds tabular governance tables |
| `api_endpoint_ext` | O | Our wrapper |
| `api_field_metadata` | O | Field identity, in_request/in_response, writable/readable, referenced_endpoint_rk |
| `api_field_description` | O | User-authored |
| `api_field_programmatic_description` | O | Extractor-authored |

### 2.4 T4 assets — function subtype

| Table | Owner | Purpose |
|---|---|---|
| `function_metadata` | O | Function identity, language, return type, definition body |
| `function_parameter` | O | Per-parameter signature |
| `function_description` | O | User-authored |
| `function_programmatic_description` | O | Extractor-authored |

### 2.5 T4 assets — metric subtype

| Table | Owner | Purpose |
|---|---|---|
| `metric_metadata` | O | Metric identity, expression, primary_asset_rk, semantic_layer_source |
| `metric_dimension` | O | Per-dimension definitions |
| `metric_description` | O | User-authored |
| `metric_programmatic_description` | O | Extractor-authored |

### 2.6 T6 — code lists / values

| Table | Owner | Purpose |
|---|---|---|
| `code_list` | O | Enum / lookup container |
| `code_value` | O | Individual values with labels |

### 2.7 Cross-cutting spine

| Table | Owner | Purpose |
|---|---|---|
| `tag` | A | Tag vocabulary |
| `badge` | A | Badge vocabulary |
| `mdl_bindings` | O | Joins `schema_rk` to MDL filenames |
| `lineage_edge` | O | Single kind-aware edge table covering all lineage relationships |

### 2.8 Knowledge layer (cards + bindings + ontology)

| Table | Owner | Purpose |
|---|---|---|
| `card` | O | Versioned card storage; `(tenant_id, layer, kind, id, version)` PK; `valid_from`/`valid_to` |
| `card_ref` | O | Inverse-ref index for traversal |
| `semantic_bindings` | O | Per-asset: extraction provenance, propagated markings, primary object_type derivation source |
| `binding_field` | O | Per-column ↔ card_field bindings with `binding_kind` and `card_version_seen` |
| `binding_interface` | O | Asset → implemented interfaces |
| `binding_causal_participation` | O | Asset → causal_node role (subject/outcome/mediator) |
| `binding_drift_flag` | O | Open drift flags (card_version_drift / field_missing_in_asset / unbound_field_added) |
| `canonical_entity` | O | CDM canonical entity registry (Employee, Patient, Asset, ...) |
| `entity_binding` | O | Asset|column → canonical_entity |
| `equivalence_class` | O | Cross-source equivalence groups |
| `equivalence_member` | O | Column → equivalence_class |
| `causal_candidate` | O | Proposed causal edges + evidence + confidence + status |
| `claim` | O | Provenance-anchored facts (mirrors `ontology_foundry.models.ClaimArtifact`) |

### 2.9 Annotation provenance

| Table | Owner | Purpose |
|---|---|---|
| `asset_annotation_provenance` | O | Append-only per-write history: `source` (llm_enrichment/rule_*/human), `confidence`, `written_by`, `written_at` |

### 2.10 Sync meta layer — runtime state

| Table | Owner | Purpose |
|---|---|---|
| `source_sync_state` | O | Per-source: `sync_mode`, `detector_kind`, `events_installed`, `last_checkpoint`, `worker_heartbeat_at`, `consecutive_failures`, `paused_reason` |
| `asset_sync_state` | O | Per-asset: `upstream_state_hash` vs `storage_state_hash`, `hashes_match` (generated), `needs_reenrichment`, `flagged_drift`, `lifecycle_state` |

### 2.11 Sync meta layer — work in flight

| Table | Owner | Purpose |
|---|---|---|
| `reindex_queue` | O | Task queue: qdrant_card / qdrant_asset / bundle_asset / bundle_catalog / lineage_derive / annotation_enrich |
| `bundle_emit_state` | O | Per-asset: `manifest_sha256`, `emitted_at`, `last_inputs_hash` |
| `catalog_emit_state` | O | Per-catalog index emission state |
| `qdrant_sync_state` | O | Per (collection, point_id): `narrative_hash`, `payload_hash`, `last_indexed_at` |

### 2.12 Sync meta layer — audit

| Table | Owner | Purpose |
|---|---|---|
| `hierarchy_audit` | O | Every mutation through HierarchyStore; covers all tiers |
| `sync_event_log` | O | Append-only log of source change events with applied outcomes |
| `mcp_audit` | O | Every MCP tool call with org/user/role/outcome |

### 2.13 Drift signals

| Table | Owner | Purpose |
|---|---|---|
| `binding_drift_flag` | O | (listed above in §2.8; repeated for index) |
| `publisher_drift` | O | Per-publisher target ↔ foundry mismatches |

### 2.14 Publisher state

| Table | Owner | Purpose |
|---|---|---|
| `publisher_state` | O | Per (publisher_name, asset_rk): `target_id`, `published_manifest_sha256`, `fidelity`, `last_error` |

### 2.15 Retrieval v2 — new tables

| Table | Owner | Purpose |
|---|---|---|
| `sql_pair` | O | (question, sql, instructions, references_asset_rks, concepts, key_areas, source_provenance) |
| `historical_qa` | O | Past `ask` tool calls: (question, answer_summary, cited_asset_rks, used_intent, used_anchors, satisfaction) |
| `legacy_project_translation` | O | Old `project_id` → (concepts, key_areas, source_ids) for backward compat |

---

## 3. Qdrant collections — full inventory

| Collection | Scope | Point id | Embedding source | Key payload filters |
|---|---|---|---|---|
| `hier_t0_orgs_<env>` | All orgs | `org_id` | `display_name` + `business_context` + `industry` + `sub_industry` | `industry`, `compliance_regimes`, `org_size_class` |
| `hier_t1_sources_<env>` | All sources | `source_id` | `display_name` + `purpose` + `business_context` + `role` + active entity-claim entities | `org_id`, `kind`, `role`, `environment` |
| `hier_t2_catalogs_<env>` | All catalogs | `catalog_uid` | `display_name` + `description` + `purpose` + `notes` | `org_id`, `source_id`, `lifecycle_stage`, `access_pattern` |
| `hier_t3_schemas_<env>` | All schemas | `schema_rk` | `display_name` + `description` + `purpose` + `domain_tags` | `org_id`, `source_id`, `catalog_uid`, `domain_tags`, `lifecycle_stage` |
| `hier_t4_assets_<env>` | All asset subtypes (single, with `asset_kind` filter) | `rk` | name + description + `purpose` + view DDL summary + bound `object_type` card body excerpt | `asset_kind`, `lifecycle_stage`, `effective_sensitivity_class`, `domain_tags`, `concepts`, `key_areas`, `causal_relations`, `org_id`, `source_id`, `catalog_uid`, `schema_rk`, `primary_object_type`, `implements_interfaces` |
| `hier_t5_fields_<env>` | All field subtypes | `rk` | name + description + `semantic_unit` + bound `card_field` mention | `field_kind`, `is_pii`, `pii_categories`, parent_rk, `org_id` |
| `hier_t6_codes_<env>` | Code lists + values | `rk` | code-list name + description + value labels (joined) | `parent_rk`, `parent_kind`, `is_closed`, `org_id` |
| `cards_<tenant_id>` | Per-tenant cards | `{tenant}::{layer}::{kind}::{id}` | card body + `aliases` | `layer`, `kind`, `markings`, `refs`, `origin`, `deprecated` |
| `sql_pairs_<tenant_id>` | Per-tenant SQL pairs | `sha256(normalized_question)` | question (and optionally SQL) | `references_asset_rks`, `concepts`, `key_areas`, `source_provenance`, `valid_for_lifecycle` |
| `historical_qa_<tenant_id>` | Per-tenant Q&A history | `qa_id` | question | `cited_asset_rks`, `used_intent`, `satisfaction`, `asked_at` |

Embedding model: `text-embedding-3-small` (same across the foundry; matches the legacy retrieval module).

**Per-env vs per-tenant naming convention:**
- `hier_t*_<env>` — environment-wide (dev/staging/prod); payload's `org_id` field provides tenant filtering.
- `cards_<tenant_id>`, `sql_pairs_<tenant_id>`, `historical_qa_<tenant_id>` — per-tenant collections (cards because authorship is fully tenant-bound; sql_pairs and historical_qa because they're high-cardinality user-authored).

---

## 4. Filesystem artifacts

### 4.1 Authored — source of truth on disk

```
tenants/<org_id>/
  organization.yaml                                       # T0
  sources/<source_id>/source.yaml                         # T1
  sources/<source_id>/catalogs/<catalog>/catalog.yaml     # T2
  sources/<source_id>/catalogs/<catalog>/schemas/<schema>.yaml   # T3
  semantic_layer/
    object_types/<id>.card.md                             # Cards
    interfaces/<id>.card.md
    causal_nodes/<id>.card.md
    derived_states/<id>.card.md
    actions/<id>.card.md
    metrics/<id>.card.md
    events/<id>.card.md
    instructions/<id>.card.md                             # Retrieval v2 promoted kind
  key_areas_vocab.yaml
  markings_vocab.yaml
  sync_config.yaml
  pipeline_config.yaml
  publishers.yaml
  mcp_config.yaml
  pack_pinning.yaml
```

### 4.2 Derived — emitted artifacts

```
tenants/<org_id>/
  assets/<source>/<schema>/<asset>/
    mdl.json
    context.json
    semantic_bindings.json
    governance.json
    causal.json
    metrics.json
    bundle_manifest.json
  catalogs/<source>/<catalog>/
    catalog.json
    catalog_assets_index.json
  causal_graph/
    claims/<claim_id>.json
    candidates/<candidate_id>.json
```

---

## 5. Retrieval mechanism — overview

The v2 retrieval module sits between callers (MCP tools, CSOD workflow nodes, compliance skill) and the storage stack. Its job is to:

1. Resolve a `RetrievalScope` (org / source / catalog / schema / concepts / key_areas / asset_kind / sensitivity_max).
2. Route the query to the right backing store(s).
3. Return shaped results (`TableContext`, `CardHit`, `SqlPairHit`, etc.) or assembled context.

### 5.1 Module layout

```
ontology_foundry/consumer/retrieval/
  retrieval_helper.py            # RetrievalHelperV2 — the façade
  scope.py                       # RetrievalScope + legacy translation
  models.py                      # shared result types
  asset_retrieval.py             # AssetRetrieval     → hier_t4_assets + Postgres bundle reads
  card_retrieval.py              # CardRetrieval      → cards_<tenant>
  sql_pairs_retrieval.py         # SqlPairsRetrievalV2 → sql_pairs_<tenant> + sql_pair table
  instructions_retrieval.py      # InstructionsRetrievalV2 → cards (kind=instruction) + legacy InstructionService
  historical_qa_retrieval.py     # HistoricalQARetrievalV2 → historical_qa_<tenant>
  sql_functions_retrieval.py     # asset_retrieval(asset_kind='function')
  metric_retrieval.py            # asset_retrieval(asset_kind='metric') + metrics.json sidecar
  lineage_retrieval.py           # lineage_edge traversal
  claim_retrieval.py             # claim + causal_candidate reads
  code_list_retrieval.py         # code_list / code_value reads
  schema_pruning.py              # Token-budgeted truncation
```

### 5.2 The scope model

```python
@dataclass
class RetrievalScope:
    org_id:               str                              # required
    source_ids:           list[str] | None = None
    catalog_uids:         list[str] | None = None
    schema_rks:           list[str] | None = None
    concepts:             list[str] | None = None          # card ids
    key_areas:            list[str] | None = None
    causal_relations:     list[str] | None = None
    lifecycle_stages:     list[str] | None = None
    include_deprecated:   bool = False
    asset_kinds:          list[str] | None = None
    sensitivity_max:      str | None = None
    compliance_regimes:   list[str] | None = None
    legacy_project_id:    str | None = None                # backward-compat
```

### 5.3 Retrieval routing — where each store fits

| Retriever | Postgres tables read | Qdrant collections queried | LLM used |
|---|---|---|---|
| `AssetRetrieval` | `v_asset` + `v_asset_effective` + `table_ext` + ext tables for active scope | `hier_t4_assets_<env>` | Optional: column selection (opt-in) |
| `CardRetrieval` | `card` + `card_ref` | `cards_<tenant>` | No |
| `SqlPairsRetrievalV2` | `sql_pair` | `sql_pairs_<tenant>` | No |
| `InstructionsRetrievalV2` | `card` (kind=instruction) + legacy `instruction` table | `cards_<tenant>` (filtered) + legacy | No |
| `HistoricalQARetrievalV2` | `historical_qa` | `historical_qa_<tenant>` | No |
| `LineageRetrievalV2` | `lineage_edge` + `v_asset` | None (pure graph) | No |
| `ClaimRetrievalV2` | `claim` + `causal_candidate` + bundle's `causal.json` | None | No |
| `CodeListRetrievalV2` | `code_list` + `code_value` | `hier_t6_codes_<env>` | No |
| `MetricRetrievalV2` | `metric_metadata` + `metric_dimension` + bundle's `metrics.json` | `hier_t4_assets_<env>` filtered to `asset_kind=metric` | No |
| `SqlFunctionsRetrievalV2` | `function_metadata` + `function_parameter` | `hier_t4_assets_<env>` filtered to `asset_kind=function` | No |
| `RetrievalHelperV2.search` (assembled) | All of the above as needed | All of the above + `cards_<tenant>` | One LLM call (synthesis, via OntologyContextLoader recipe) |

---

## 6. Read patterns by question type

A consumer-facing map: given a question shape, which retriever is the right entry point.

| Question shape | Entry point | Underlying stores |
|---|---|---|
| "What tables / endpoints have X?" | `AssetRetrieval.search(query, scope)` | `hier_t4_assets` + Postgres reads |
| "What concept means Y?" | `CardRetrieval.search(query, scope, kinds=['object_type'])` | `cards_<tenant>` + `card` table |
| "Why does Z happen?" (causal) | `RetrievalHelperV2.search(query, scope, intent=CAUSAL_REASONING)` | All — assembled via `OntologyContextLoader` |
| "Give me sample SQL for question like Q" | `SqlPairsRetrievalV2.search(query, scope)` | `sql_pairs_<tenant>` |
| "What instructions apply to PHI handling?" | `InstructionsRetrievalV2.search(query, scope)` | `cards_<tenant>` (kind=instruction) + legacy |
| "What has someone asked before about W?" | `HistoricalQARetrievalV2.search(query, scope)` | `historical_qa_<tenant>` |
| "Where does this asset's data come from?" | `LineageRetrievalV2.trace(asset_rk, direction='upstream')` | `lineage_edge` |
| "What enums does this column have?" | `CodeListRetrievalV2.for_column(column_rk)` | `code_list` + `code_value` |
| "Which metrics measure V?" | `MetricRetrievalV2.search(query, scope)` | `metric_metadata` + `hier_t4_assets` |
| "What causal claims involve this asset?" | `ClaimRetrievalV2.by_asset(asset_rk)` | bundle's `causal.json` + `claim` table |
| "Compare how Employee is modeled across sources" | `RetrievalHelperV2.search(query, scope, intent=ENTITY_RESOLUTION)` | Equivalence + bindings + bundles |
| "Recommend a compliance dashboard" | `RetrievalHelperV2.search(query, scope, intent=COMPLIANCE_REC)` | All — wide-fanout assembled context |

---

## 7. Example retrievals — eight worked outcomes

Each example shows: the question, the resolved scope, the retriever call(s), and the shaped outcome the caller receives.

### 7.1 Example: asset search by concept

**Question:** "Which tables in CSOD hold employee training completion data?"

**Scope resolution:**
```python
scope = RetrievalScope(
    org_id="acme-corp",
    source_ids=["csod-servicenow-local"],
    concepts=["employee", "training_assignment"],         # resolved from anchor_resolution_pre
    asset_kinds=["table", "view"],
    lifecycle_stages=["production"],
)
```

**Retriever call:**
```python
hits = await helper.get_table_names_and_schema_contexts(
    query="employee training completion data",
    scope=scope,
    k=5,
)
```

**Stores hit:**
- Qdrant `hier_t4_assets_prod` — vector search with payload filter `asset_kind ∈ {table, view}`, `concepts && [employee, training_assignment]`, `source_id = csod-servicenow-local`, `lifecycle_stage = production`.
- Postgres `v_asset` + `table_ext` + `column_metadata` — hydrate top hits with column info.

**Outcome:**
```python
[
  TableContext(
    asset_rk="postgres://csod-servicenow-local/public/training_assignment",
    asset_kind="table",
    name="training_assignment",
    score=0.91,
    concepts=["training_assignment", "employee"],
    key_areas=["Training_Compliance", "Workforce"],
    causal_relations=["overdue_risk", "compliance_gap"],
    effective_sensitivity_class="confidential",
    description="Per-employee training assignment with assigned_date, due_date, completed_date.",
    primary_object_type="training_assignment",
    columns=[
      ColumnInfo(name="assignment_id", type="INTEGER", is_business_key=True),
      ColumnInfo(name="employee_id", type="INTEGER"),
      ColumnInfo(name="course_id", type="INTEGER"),
      ColumnInfo(name="assigned_date", type="TIMESTAMP"),
      ColumnInfo(name="due_date", type="TIMESTAMP"),
      ColumnInfo(name="completed_date", type="TIMESTAMP", is_pii=False),
      ColumnInfo(name="status", type="VARCHAR", code_list_rk="codelist://.../status"),
    ],
  ),
  TableContext(
    asset_rk="postgres://csod-servicenow-local/public/learning_activity",
    score=0.86,
    concepts=["learning_activity"],
    ...
  ),
  TableContext(
    asset_rk="postgres://csod-servicenow-local/public/certification_core",
    score=0.74,
    ...
  ),
]
```

### 7.2 Example: causal-reasoning assembled context

**Question:** "Why are clinical staff failing HIPAA training?"

**Scope resolution:**
```python
scope = RetrievalScope(
    org_id="acme-corp",
    source_ids=["csod-servicenow-local"],
    concepts=["employee", "training_assignment", "compliance_gap"],
    key_areas=["HIPAA", "Training_Compliance", "Clinical_Operations"],
    causal_relations=["overdue_risk", "compliance_gap"],
)
```

**Retriever call:**
```python
ctx = await helper.search(
    query="Why are clinical staff failing HIPAA training?",
    scope=scope,
    intent=ContextIntent.CAUSAL_REASONING,
    k=10,
)
```

**Stores hit (under the hood, via `OntologyContextLoader`):**
- `cards_<tenant>` (vector + kind filter) — finds `employee`, `training_assignment`, `compliance_gap`, `overdue_risk`, `late_completion`, `auditable` cards.
- `card_ref` (traversal) — walks `compliance_gap.subject_refs` and `overdue_risk.outcome_refs` for 2 hops.
- `hier_t4_assets_prod` (filtered) — pulls the 3 most-bound assets.
- Postgres bundle reads — `mdl.json`, `semantic_bindings.json`, `causal.json` per asset.
- `claim` table — claims referencing any of the anchor cards.

**Outcome (`AssembledContext` shape):**
```python
AssembledContext(
  intent=ContextIntent.CAUSAL_REASONING,
  anchors=[
    AssetAnchor(rk="postgres://csod-servicenow-local/public/training_assignment"),
    CardAnchor(card_id="compliance_gap", kind="causal_node"),
  ],
  cards_full=[
    CardView(id="employee", kind="object_type", body="..."),
    CardView(id="training_assignment", kind="object_type", body="..."),
    CardView(id="compliance_gap", kind="causal_node", body="..."),
    CardView(id="overdue_risk", kind="causal_node", body="..."),
  ],
  cards_summary=[
    CardSummary(id="late_completion", kind="derived_state", excerpt="A LateCompletion is..."),
    CardSummary(id="auditable", kind="interface", excerpt="An Auditable object..."),
  ],
  cards_manifest=[
    CardManifestEntry(id="overdue_assignment", kind="derived_state", title="OverdueAssignment"),
    CardManifestEntry(id="phishing_risk", kind="causal_node", title="PhishingRisk"),
    CardManifestEntry(id="department", kind="object_type", title="Department"),
  ],
  bundles={
    "postgres://...training_assignment": AssetBundle(...),
    "postgres://...csod_employee": AssetBundle(...),
    "postgres://...dept_compliance_rollup": AssetBundle(...),
  },
  warnings=[
    ContextWarning(kind="low_confidence_claims", asset_rk="postgres://...training_assignment",
                   detail="3 claims included with confidence < 0.6"),
  ],
  estimated_tokens=9420,
  demotions_applied=["branching cap hit at hop 1; demoted 2 cards to summary"],
)
```

The downstream LLM call (e.g., MCP `ask` tool) uses `ctx.render_prompt()` and produces a synthesized answer with citations.

### 7.3 Example: SQL pairs lookup

**Question:** "Show me example SQL for revenue queries."

**Scope:**
```python
scope = RetrievalScope(
    org_id="acme-corp",
    concepts=["revenue", "monetary_amount"],
    key_areas=["Revenue_Cycle", "Finance"],
)
```

**Retriever call:**
```python
pairs = await helper.get_sql_pairs(
    query="revenue queries",
    scope=scope,
    k=5,
)
```

**Stores hit:**
- Qdrant `sql_pairs_<tenant>` — vector search on question text with payload filter `concepts && scope.concepts`, `key_areas && scope.key_areas`, `valid_for_lifecycle != deprecated`.
- Postgres `sql_pair` — hydrate full SQL + instructions.

**Outcome:**
```python
[
  SqlPairHit(
    sql_pair_id="sp-revenue-001",
    question="What was our monthly revenue for the last 12 months?",
    sql="SELECT DATE_TRUNC('month', order_dt) AS month, SUM(total_amount * fx_to_usd(currency, order_dt)) AS revenue_usd FROM orders WHERE order_dt >= CURRENT_DATE - INTERVAL '12 months' GROUP BY 1 ORDER BY 1",
    instructions="Use fx_to_usd to normalize across currencies. Excludes cancelled orders.",
    references_asset_rks=[
      "snowflake://acme-prod.analytics.finance_marts/orders",
      "function://acme-snowflake-prod/finance_marts/fn_fx_to_usd(8c2d4a)",
    ],
    score=0.93,
    source_provenance="authored",
  ),
  SqlPairHit(
    sql_pair_id="sp-revenue-002",
    question="Revenue by customer segment for current quarter",
    sql="SELECT c.segment, SUM(o.total_amount) AS revenue FROM orders o JOIN customers c ON c.id = o.customer_id WHERE o.order_dt >= DATE_TRUNC('quarter', CURRENT_DATE) GROUP BY 1",
    score=0.81,
    source_provenance="imported_legacy",
  ),
  ...
]
```

### 7.4 Example: instructions retrieval (PHI handling)

**Question:** "What instructions apply when handling PHI fields?"

**Scope:**
```python
scope = RetrievalScope(
    org_id="acme-corp",
    concepts=["patient", "encounter"],
    key_areas=["HIPAA"],
    compliance_regimes=["HIPAA"],
)
```

**Retriever call:**
```python
instructions = await helper.get_instructions(
    query="PHI field handling",
    scope=scope,
    k=10,
)
```

**Stores hit:**
- Qdrant `cards_<tenant>` — filter to `kind=instruction`, with payload `applies_to_concepts && scope.concepts OR applies_to_key_areas && scope.key_areas`.
- Postgres `card` — full body for top hits.
- Legacy `instruction` (DomainWorkflowService) — fallback for org-wide directives.

**Outcome:**
```python
[
  InstructionHit(
    instruction_id="phi_field_access_logging",
    title="PHI field access must be logged",
    body="PHI field access must be logged with the requesting user_id, timestamp, and purpose. All queries that return PHI columns must produce a row in the access_log table. Bulk exports require additional approval from the compliance officer.",
    scope_concepts=["patient", "encounter"],
    scope_key_areas=["HIPAA"],
    score=0.94,
    source="card:instruction",
  ),
  InstructionHit(
    instruction_id="phi_masking_in_non_prod",
    title="PHI must be masked in non-production environments",
    body="When data containing PHI is replicated to dev/staging environments, the configured masking rules in masking_rules.yaml must be applied to every PHI-categorized column.",
    scope_key_areas=["HIPAA"],
    score=0.87,
    source="card:instruction",
  ),
  InstructionHit(
    instruction_id="legacy-001",
    title="Legacy: PHI export requires SVP signoff",
    body="(legacy directive) ...",
    score=0.74,
    source="legacy_instruction_service",
  ),
]
```

### 7.5 Example: metric retrieval anchored on key_area

**Question:** "Which metrics measure HIPAA training compliance for the clinical org?"

**Scope:**
```python
scope = RetrievalScope(
    org_id="acme-corp",
    key_areas=["HIPAA", "Training_Compliance", "Clinical_Operations"],
    causal_relations=["compliance_gap", "overdue_risk"],
)
```

**Retriever call:**
```python
metrics = await helper.get_metrics(query="HIPAA training compliance clinical", scope=scope)
```

**Stores hit:**
- Qdrant `hier_t4_assets_prod` filtered to `asset_kind=metric`, payload filter on `key_areas && scope.key_areas OR causal_relations && scope.causal_relations`.
- Postgres `metric_metadata` + `metric_dimension` + bundle's `metrics.json`.

**Outcome:**
```python
[
  MetricBundle(
    metric_rk="metric://acme-snowflake-prod/clinical_marts/training_completion_rate",
    name="training_completion_rate",
    definition_kind="ratio",
    expression="COUNT_IF(completed_date IS NOT NULL) / COUNT(*)",
    primary_object_type="training_assignment",
    key_areas=["Training_Compliance", "HIPAA"],
    causal_relations=["compliance_gap"],
    dimensions=[
      MetricDimension(name="department",  from_field_rk="..."),
      MetricDimension(name="time",        default_grain="day"),
      MetricDimension(name="course_type", from_field_rk="..."),
    ],
    default_time_grain="day",
    format={"kind": "percentage"},
    primary_asset_rk="postgres://csod-servicenow-local/public/training_assignment",
    semantic_layer_source="dbt_semantic",
    score=0.92,
  ),
  MetricBundle(
    metric_rk="metric://...overdue_training_count",
    name="overdue_training_count",
    definition_kind="count",
    expression="COUNT_IF(status = 'overdue')",
    primary_object_type="training_assignment",
    key_areas=["Training_Compliance"],
    causal_relations=["overdue_risk", "compliance_gap"],
    score=0.88,
  ),
  ...
]
```

### 7.6 Example: lineage trace

**Question:** "Where does the data in dim_employee come from?"

**Retriever call:**
```python
lineage = await helper.get_lineage(
    asset_rk="snowflake://acme-prod.analytics.workforce_marts/dim_employee",
    direction="upstream",
    max_hops=3,
)
```

**Stores hit:**
- Postgres `lineage_edge` — recursive CTE for upstream traversal up to 3 hops.
- Postgres `v_asset` — hydrate node summaries.

**Outcome:**
```python
LineageGraph(
  root_rk="snowflake://acme-prod.analytics.workforce_marts/dim_employee",
  nodes=[
    LineageNode(rk="snowflake://acme-prod.analytics.workforce_marts/dim_employee",       hop=0, kind="table"),
    LineageNode(rk="snowflake://acme-prod.staging.workforce_marts/stg_employee",         hop=1, kind="table"),
    LineageNode(rk="api://acme-workday/standard_objects/Worker",                          hop=2, kind="api_endpoint"),
    LineageNode(rk="postgres://csod-servicenow-local/public/csod_employee",               hop=1, kind="table"),
    LineageNode(rk="snowflake://acme-prod.raw.csod_replica/csod_employee",                hop=2, kind="table"),
  ],
  edges=[
    LineageEdge(from_rk="...stg_employee", to_rk="...dim_employee",
                edge_kind="derived_from", evidence_kind="declared_view_ddl"),
    LineageEdge(from_rk="api://...Worker", to_rk="...stg_employee",
                edge_kind="replicated_from", evidence_kind="extracted_dbt",
                pipeline_ref="fivetran-workday-to-snowflake"),
    LineageEdge(from_rk="postgres://...csod_employee", to_rk="...dim_employee",
                edge_kind="depends_on", evidence_kind="declared_fk"),
    LineageEdge(from_rk="...csod_replica/csod_employee", to_rk="postgres://...csod_employee",
                edge_kind="replicated_from", evidence_kind="observed_dag"),
  ],
)
```

### 7.7 Example: concept search

**Question:** "What concepts relate to attrition risk?"

**Scope:**
```python
scope = RetrievalScope(org_id="acme-corp")
```

**Retriever call:**
```python
cards = await helper.get_concepts(query="attrition risk", scope=scope, k=10)
```

**Stores hit:**
- Qdrant `cards_<tenant>` — vector search, kind filter to `object_type` and `causal_node`.

**Outcome:**
```python
[
  CardHit(id="attrition_risk", kind="causal_node",   score=0.94,
          frontmatter={"subject_refs": ["employee","training_assignment","manager"],
                       "outcome_refs": ["compliance_gap","department"]},
          excerpt="AttritionRisk is the per-employee risk that the employee leaves..."),
  CardHit(id="overdue_risk",   kind="causal_node",   score=0.81,
          excerpt="OverdueRisk is the per-employee risk that one or more required training..."),
  CardHit(id="employee",       kind="object_type",   score=0.74,
          excerpt="An Employee is a person who works at the organization..."),
  CardHit(id="late_completion", kind="derived_state", score=0.66,
          excerpt="A LateCompletion is a TrainingAssignment whose completed_date > due_date..."),
  ...
]
```

### 7.8 Example: cross-source entity resolution

**Question:** "How is Employee modeled across CSOD and Workday?"

**Scope:**
```python
scope = RetrievalScope(
    org_id="acme-corp",
    concepts=["employee"],
    source_ids=["csod-servicenow-local", "acme-workday"],
)
```

**Retriever call:**
```python
ctx = await helper.search(
    query="How is Employee modeled across CSOD and Workday",
    scope=scope,
    intent=ContextIntent.ENTITY_RESOLUTION,
)
```

**Stores hit:**
- `cards_<tenant>` — pull the `employee` card.
- Postgres `equivalence_class` + `equivalence_member` — find the equivalence class for the Employee concept across the two sources.
- `v_asset` + bundle reads — fetch the bound asset(s) per source.
- `semantic_bindings` + `binding_field` — pull field-level alignment between CSOD's `csod_employee` and Workday's `Worker`.

**Outcome:**
```python
AssembledContext(
  intent=ContextIntent.ENTITY_RESOLUTION,
  anchors=[CardAnchor(card_id="employee", kind="object_type")],
  cards_full=[
    CardView(id="employee", kind="object_type", body="..."),
  ],
  bundles={
    "postgres://csod-servicenow-local/public/csod_employee": AssetBundle(...),
    "api://acme-workday/standard_objects/Worker": AssetBundle(...),
  },
  cross_source_alignment=EquivalenceClassView(
    class_id="ec-employee-identity-001",
    members=[
      EquivalenceMember(
        column_rk="postgres://csod-servicenow-local/public/csod_employee/EmployeeID",
        source_id="csod-servicenow-local",
        binding_kind="identity",
      ),
      EquivalenceMember(
        column_rk="api://acme-workday/standard_objects/Worker/Worker_ID",
        source_id="acme-workday",
        binding_kind="identity",
      ),
      EquivalenceMember(
        column_rk="snowflake://acme-prod.analytics.workforce_marts/dim_employee/employee_id",
        source_id="acme-snowflake-prod",
        binding_kind="identity",
      ),
    ],
    field_alignment=[
      FieldAlignment(card_field="employee_id", csod="EmployeeID",       workday="Worker_ID"),
      FieldAlignment(card_field="department_id", csod="DepartmentID",   workday="Organization_ID"),
      FieldAlignment(card_field="manager_id",  csod="ManagerID",        workday="Manager_Worker_Reference"),
      FieldAlignment(card_field="employment_status", csod="Status",     workday="Worker_Status"),
    ],
  ),
  estimated_tokens=4820,
)
```

---

## 8. Vector store provisioning checklist

For a new environment / new tenant onboarding:

- [ ] Provision Qdrant cluster; verify network reachable.
- [ ] Create per-environment collections:
  - [ ] `hier_t0_orgs_<env>`
  - [ ] `hier_t1_sources_<env>`
  - [ ] `hier_t2_catalogs_<env>`
  - [ ] `hier_t3_schemas_<env>`
  - [ ] `hier_t4_assets_<env>`
  - [ ] `hier_t5_fields_<env>`
  - [ ] `hier_t6_codes_<env>`
- [ ] On first-tenant onboarding, create per-tenant collections:
  - [ ] `cards_<tenant_id>`
  - [ ] `sql_pairs_<tenant_id>`
  - [ ] `historical_qa_<tenant_id>`
- [ ] Set collection-level payload indexes for the filters listed in §3.
- [ ] Verify `qdrant_sync_state` is updated by reindex worker after the first ingest.
- [ ] Run smoke test: `search_assets` returns ≥1 result for a known fixture asset.

---

## 9. Operational lookups — quick reference

| Question | Query |
|---|---|
| Sync worker alive? | `SELECT worker_heartbeat_at, sync_mode, paused_reason FROM source_sync_state WHERE source_id = $1` |
| Drift count? | `SELECT COUNT(*) FROM asset_sync_state WHERE NOT hashes_match` |
| Queue depth? | `SELECT task_kind, status, COUNT(*) FROM reindex_queue GROUP BY 1,2` |
| Last bundle regen? | `SELECT emitted_at, manifest_sha256 FROM bundle_emit_state WHERE asset_rk = $1` |
| Last Purview publish? | `SELECT asset_rk, last_published_at FROM publisher_state WHERE publisher_name = $1` |
| Annotation provenance? | `SELECT * FROM asset_annotation_provenance WHERE asset_rk = $1 AND field = $2 ORDER BY written_at DESC` |
| MCP query history? | `SELECT user_id, tool_name, occurred_at FROM mcp_audit WHERE context_loaded->>'rk' = $1 ORDER BY occurred_at DESC LIMIT 50` |
| Recent source changes? | `SELECT * FROM sync_event_log WHERE source_id = $1 AND detected_at > now() - interval '1 day'` |
| Bindings drift open? | `SELECT * FROM binding_drift_flag WHERE resolved_at IS NULL ORDER BY first_observed_at DESC` |
| Publisher drift? | `SELECT * FROM publisher_drift WHERE resolved_at IS NULL` |

---

## 10. Per-section spec cross-references

| Section here | Authoritative spec |
|---|---|
| §2.1 spine T0–T1 | `T0_T1_organization_source_spec.md` + addendum |
| §2.1 spine T2–T3 | `T2_to_T6_amundsenrds_sidecar_spec.md` §4–5 |
| §2.2–§2.5 T4 subtypes | `T2_to_T6_amundsenrds_sidecar_spec.md` §6 |
| §2.6 T6 | `T2_to_T6_amundsenrds_sidecar_spec.md` §8 |
| §2.7 lineage_edge | `T2_to_T6_amundsenrds_sidecar_spec.md` §10 |
| §2.8 knowledge layer | `semantic_layer_card_spec.md` §9 |
| §2.9 annotation provenance | `mdl_table_concept_annotation_spec.md` §4.2 |
| §2.10–§2.13 sync meta | `live_sync_pipeline_spec.md` §4, `hierarchy_persistence_and_ingestion_spec.md` §4–8 |
| §2.14 publisher state | `bundle_publishers_spec.md` §2.4 |
| §2.15 retrieval v2 tables | `retrieval_v2_spec.md` §5.3, §5.5, §3.2 |
| §3 Qdrant collections | `hierarchy_persistence_and_ingestion_spec.md` §5, `semantic_layer_card_spec.md` §9.3, `retrieval_v2_spec.md` §5.3 |
| §4 filesystem | `mdl_bundle_spec.md` §2, `semantic_layer_card_spec.md` §2.1, multiple |
| §5 retrieval | `retrieval_v2_spec.md` |
| §7 example retrievals | This doc; pulls from retrieval v2 + consumer api spec |

---

## 11. Spec index

The 15 specs (~7,700 lines total) in topological order:

1. `T0_T1_organization_source_spec.md` — Organization & Source declarative model.
2. `T0_T1_addendum_amundsenrds_linkage.md` — `cluster_rk` + synthetic clusters for API sources.
3. `semantic_layer_card_spec.md` — Card format, kinds, refs, markings.
4. `T2_to_T6_amundsenrds_sidecar_spec.md` — Spine storage on amundsenrds + sidecars.
5. `mdl_bundle_spec.md` — Per-asset bundle wire format.
6. `mdl_table_concept_annotation_spec.md` — Bottoms-up concepts/key_areas/causal_relations.
7. `hierarchy_persistence_and_ingestion_spec.md` — Postgres + Qdrant ops + databuilder integration.
8. `bundle_publishers_spec.md` — Purview / Unity / DataHub mappers.
9. `bundle_consumer_api_spec.md` — BundleStore + OntologyContextLoader.
10. `evaluation_harness_spec.md` — Eval corpus + regression gates.
11. `mdl_auto_generation_from_source_spec.md` — Source → MDL pipeline.
12. `mcp_qa_agents_spec.md` — MCP server + Q&A tools.
13. `live_sync_pipeline_spec.md` — Continuous source synchronization.
14. `retrieval_v2_spec.md` — Replacement retrieval module.
15. `storage_topology_reference.md` — This document.

---

## 12. Change log

| Date | Change |
|---|---|
| 2026-05-17 | Initial consolidation. |
