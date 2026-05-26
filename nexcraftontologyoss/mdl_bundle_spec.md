# MDL Bundle — Specification

**Status:** Draft 2026-05-15.
**Part of:** Data Knowledge Hierarchy series.
**Depends on:** `T0_T1_organization_source_spec.md`, `T2_to_T6_amundsenrds_sidecar_spec.md`, `semantic_layer_card_spec.md`.
**Forward refs:** `bundle_publishers_spec.md`, `bundle_consumer_api_spec.md`, `hierarchy_persistence_and_ingestion_spec.md`.
**Leverages:** `ontology_foundry.context.TabularContextBundle` (input to MDL emission for tabular assets); `ontology_foundry.models.ClaimArtifact` and `RelationArtifact` (claims/edges flow into `causal.json` and lineage).

---

## 1. Scope

This spec defines the **per-asset bundle** — the external wire format the foundry produces. The bundle is what publishers push to Microsoft Purview / Databricks Unity Catalog / DataHub and what internal skills (Compliance, dashboard recommender) consume as input. The internal storage (`T2_to_T6_amundsenrds_sidecar_spec.md`) is upstream; the bundle is the contract.

Each asset (table, view, materialized view, API endpoint, function, metric) gets one bundle folder containing:

| File | Authority | Update cadence |
|---|---|---|
| `mdl.json` | Vendor-extracted + human-enriched | Schema change |
| `context.json` | Derived from T0–T3 storage | Hierarchy edits |
| `semantic_bindings.json` | Derived from card prose + author overrides | Card edits or rebinding |
| `governance.json` | Operational state | Ownership / sensitivity / lineage changes |
| `causal.json` | Foundry causal pipeline output | Foundry runs |
| `metrics.json` | Generated index | Metric add / remove |

Plus a catalog-level rollup:

```
catalogs/<source_id>/<catalog_name>/
  catalog.json
  catalog_assets_index.json
```

This spec defines the schemas and identity rules. Generation pipelines and storage triggers are in `hierarchy_persistence_and_ingestion_spec.md`.

---

## 2. Bundle layout

```
tenants/<org_id>/
  assets/
    <source_id>/<schema_name>/<asset_name>/
      mdl.json
      context.json
      semantic_bindings.json
      governance.json
      causal.json
      metrics.json
  catalogs/
    <source_id>/<catalog_name>/
      catalog.json
      catalog_assets_index.json
```

Asset folder name segments:
- `<source_id>` is the T1 source slug (e.g., `acme-snowflake-prod`).
- `<schema_name>` is the amundsenrds schema name. For sources whose URN bakes the catalog into the schema name (Snowflake: `database.schema`), use the joined form to preserve uniqueness.
- `<asset_name>` is the asset's name. For functions with overloads, append the signature hash: `fn_foo_3a9b1c`.

For API endpoints the same layout applies; `<asset_name>` is the endpoint name.

---

## 3. `mdl.json` — extended MDL envelope (v2)

### 3.1 Envelope

```json
{
  "mdl_version": "2.0",
  "source_id":   "acme-snowflake-prod",
  "catalog":     "analytics",
  "schema":      "clinical_marts",
  "models":     [ /* tables + views + materialized views */ ],
  "endpoints":  [ /* API endpoints */ ],
  "functions":  [ /* SQL UDFs, stored functions */ ],
  "metrics":    [ /* semantic-layer metrics */ ],
  "streams":    [ /* deferred; field reserved for forward compat */ ]
}
```

Backward compatibility: a v1 MDL with only `models[]` is a valid v2 MDL with empty `endpoints/functions/metrics/streams` and `mdl_version` upgraded. Existing genieml `sql_meta/*` MDLs continue to load.

A single MDL file usually carries **one asset's** definition: only one of the parallel arrays has one entry, the rest are empty. Multi-asset MDL files (e.g., a whole schema in one file) remain valid for legacy reasons but the foundry emits **one asset per file** going forward.

### 3.2 Model block (table / view / materialized view)

```json
{
  "name": "encounters",
  "rk":   "snowflake://acme-prod.analytics.clinical_marts/encounters",
  "description": "...",
  "is_view": false,
  "tableReference": { "table": "encounters" },
  "materialization": {
    "kind": "table",                       /* 'table'|'view'|'mv'|'mv_incremental'|'mv_scheduled' */
    "is_materialized": false
  },
  "view_definition": null,                 /* present and non-null when is_view = true */
  "columns": [
    {
      "name": "encounter_id",
      "type": "VARCHAR",
      "rk":   "snowflake://acme-prod.analytics.clinical_marts/encounters/encounter_id",
      "notNull": true,
      "is_pii": false,
      "is_business_key": true,
      "semantic_unit": null,
      "code_list_rk": null,                /* set when column has an enum */
      "properties": {
        "displayName": "Encounter ID",
        "description": "Globally unique identifier for a clinical encounter.",
        "description_provenance": "extractor:snowflake_information_schema"
      }
    }
  ]
}
```

For views/MVs, `view_definition` is populated:

```json
"view_definition": {
  "language": "sql",
  "query":    "SELECT ... FROM ...",
  "depends_on": [
    "snowflake://acme-prod.analytics.clinical_marts/patients",
    "snowflake://acme-prod.analytics.clinical_marts/encounters_raw"
  ],
  "depends_on_kinds": ["table", "table"]
}
```

`depends_on[]` populates `lineage_edge` rows with `edge_kind: depends_on`, `evidence_kind: declared_view_ddl`.

### 3.3 Endpoint block (API)

```json
{
  "name": "Account",
  "rk":   "api://acme-salesforce/standard_objects/Account",
  "description": "Customer accounts in Salesforce.",
  "endpoint_reference": {
    "base_path": "/services/data/v59.0/sobjects/Account",
    "url_template": null,
    "http_methods": ["GET", "POST", "PATCH", "DELETE"],
    "pagination_kind": "page",
    "auth_scopes": ["api"],
    "rate_limit_info": null,
    "idempotency": false,
    "request_schema_ref": null,
    "response_schema_ref": null
  },
  "fields": [
    {
      "name": "Id",
      "field_path": "Id",
      "rk": "api://acme-salesforce/standard_objects/Account/Id",
      "type": "ID",
      "is_nullable": false,
      "in_request": false, "in_response": true,
      "in_query": false, "in_path": false,
      "writable": false, "readable": true,
      "referenced_endpoint_rk": null,
      "is_pii": false,
      "semantic_unit": null,
      "code_list_rk": null,
      "properties": {
        "displayName": "Account ID",
        "description": "Salesforce-assigned unique identifier.",
        "description_provenance": "extractor:salesforce_sobject"
      }
    },
    {
      "name": "OwnerId",
      "field_path": "OwnerId",
      "rk": "api://acme-salesforce/standard_objects/Account/OwnerId",
      "type": "REFERENCE",
      "referenced_endpoint_rk": "api://acme-salesforce/standard_objects/User",
      "in_response": true, "writable": true, "readable": true,
      "properties": { ... }
    }
  ]
}
```

`referenced_endpoint_rk` populates `lineage_edge` with `edge_kind: references`.

### 3.4 Function block

```json
{
  "name": "fn_normalize_phone",
  "rk":   "function://acme-snowflake-prod/utils/fn_normalize_phone(3a9b1c)",
  "description": "Normalizes phone numbers to E.164.",
  "function_reference": {
    "language": "sql",
    "is_deterministic": true,
    "is_table_valued": false,
    "depends_on": [],
    "depends_on_kinds": []
  },
  "parameters": [
    {
      "name": "phone",
      "ordinal": 1,
      "type": "VARCHAR",
      "mode": "IN",
      "default_value": null
    }
  ],
  "return_type": "VARCHAR",
  "properties": {
    "displayName": "Normalize Phone",
    "description": "Strips formatting, validates, returns E.164.",
    "description_provenance": "extractor:snowflake_information_schema"
  }
}
```

### 3.5 Metric block

```json
{
  "name": "revenue_usd",
  "rk":   "metric://acme-snowflake-prod/finance_marts/revenue_usd",
  "description": "Sum of order amounts converted to USD at transaction-time rates.",
  "metric_reference": {
    "definition_kind": "sql_aggregation",
    "expression": "SUM(orders.total_amount * fx_to_usd(orders.currency, orders.order_dt))",
    "depends_on": [
      "snowflake://acme-prod.analytics.finance_marts/orders",
      "function://acme-snowflake-prod/finance_marts/fn_fx_to_usd(8c2d4a)"
    ],
    "depends_on_kinds": ["table", "function"],
    "primary_asset": "snowflake://acme-prod.analytics.finance_marts/orders",
    "primary_asset_kind": "table",
    "semantic_layer_source": "dbt_semantic"
  },
  "grain": "transaction",
  "dimensions": [
    { "name": "currency",  "from": "orders.currency",  "source_field_kind": "column",
      "source_field_rk": "snowflake://acme-prod.analytics.finance_marts/orders/currency" },
    { "name": "region",    "from": "orders.region",    "source_field_kind": "column", ... }
  ],
  "default_time_grain": "day",
  "format": { "kind": "currency", "currency": "USD" }
}
```

### 3.6 Description provenance

Every prose description carries a `description_provenance` field with value matching amundsenrds' `*_description.source` discriminator:
- `user` — human-authored.
- `extractor:<extractor_id>` — system-extracted from vendor metadata.
- `doc_extraction:<doc_id>` — extracted by foundry LLM pass from a document.
- `inferred:<algorithm>` — inferred from data patterns (e.g., a column profile suggesting "this looks like a phone number").

When multiple description sources exist (extractor-generated AND human-authored), the MDL carries the user-authored value; the extractor version remains queryable from amundsenrds programmatic_description tables but is not the canonical bundle representation.

---

## 4. `context.json` — T0–T3 projection

A snapshot of the org / source / catalog / schema chain at emission time. Lets a consumer answer questions about scope ("what industry is this from?", "what compliance regimes apply?") without joining back to upstream tables.

```json
{
  "context_version": "1.0",
  "rendered_at": "2026-05-15T...",
  "organization": {
    "org_id": "acme-corp",
    "industry": "healthcare",
    "sub_industry": "provider",
    "compliance_regimes": ["HIPAA", "GDPR", "SOC2"],
    "primary_language": "en-US",
    "business_context": "..."
  },
  "source": {
    "source_id": "acme-snowflake-prod",
    "kind": "snowflake",
    "role": "analytical_warehouse",
    "environment": "prod",
    "purpose": "Central analytics warehouse...",
    "business_context": "...",
    "region_id": "us"
  },
  "catalog": {
    "catalog_uid": "acme-snowflake-prod::catalog::analytics",
    "catalog_name": "analytics",
    "purpose": "Curated marts...",
    "lifecycle_stage": "production",
    "access_pattern": "read_only",
    "managed_by": "dbt"
  },
  "schema": {
    "schema_rk": "...",
    "schema_name": "clinical_marts",
    "purpose": "Clinical encounter facts and dimensions.",
    "domain_tags": ["Clinical", "HR"],
    "lifecycle_stage": "production"
  }
}
```

API-only sources may have a `catalog` block with `catalog_name: "default"` or omit it entirely; consumers should treat absence as "no catalog tier."

`context.json` is **derived**. Consumers must not author it. Storage is the source of truth; this file is regenerated whenever any upstream T0–T3 row changes (cascaded by the persistence layer; see `hierarchy_persistence_and_ingestion_spec.md`).

---

## 5. `semantic_bindings.json` — card ↔ MDL bridge

The structured companion to card prose. Every binding the card body describes ("the `employee_id` field maps to `EmployeeID`") is reified here so machines never parse prose.

```json
{
  "bindings_version": "1.0",
  "asset_rk": "snowflake://acme-prod.csod/public/csod_employee",
  "asset_kind": "table",
  "extracted_at": "2026-05-15T...",
  "extraction_provenance": "llm_from_card_body:claude-opus-4-7",
  "human_reviewed": true,
  "human_reviewer": "jane.k@acme.com",
  "human_reviewed_at": "2026-05-12T...",

  "primary_object_type": "employee",

  "field_bindings": [
    { "asset_field": "EmployeeID",
      "asset_field_rk": "snowflake://acme-prod.csod/public/csod_employee/EmployeeID",
      "card_field": "employee_id",
      "binding_kind": "identity",
      "card_id": "employee",
      "card_version_seen": 3 },
    { "asset_field": "DepartmentID",
      "asset_field_rk": "...",
      "card_field": "department_id",
      "binding_kind": "reference",
      "refs_object_type": "department",
      "card_id": "employee",
      "card_version_seen": 3 },
    { "asset_field": "ManagerID",
      "asset_field_rk": "...",
      "card_field": "manager_id",
      "binding_kind": "self_reference",
      "card_id": "employee",
      "card_version_seen": 3 }
  ],

  "implements_interfaces": [
    { "interface_id": "trainable",  "via_object_type": "employee" },
    { "interface_id": "auditable",  "via_object_type": "employee" }
  ],

  "causal_participation": [
    { "causal_node_id": "overdue_risk",     "role": "subject" },
    { "causal_node_id": "compliance_gap",   "role": "subject" },
    { "causal_node_id": "phishing_risk",    "role": "subject" }
  ],

  "propagated_markings": ["contains_pii"],

  "drift_flags": []
}
```

### 5.1 `binding_kind` vocabulary

| `binding_kind` | Meaning |
|---|---|
| `identity` | The field uniquely identifies the bound object_type (primary key for the entity). |
| `attribute` | The field is one of the bound entity's properties. |
| `reference` | The field references another object_type (foreign-key shape). `refs_object_type` required. |
| `self_reference` | Reference to another instance of the same object_type (e.g., `manager_id` on `employee`). |
| `discriminator` | The field's value selects which sub-type of the bound entity applies. |
| `temporal` | The field is a timestamp anchoring the row in time. |

### 5.2 `card_version_seen`

Captures which card version the binding was extracted against. If the card is later edited (version bumps), the foundry compares the current card body to `card_version_seen` and flags a `drift_flag` if the binding needs re-extraction.

### 5.3 Drift flags

```json
"drift_flags": [
  { "kind": "card_version_drift",
    "card_id": "employee",
    "seen_version": 3,
    "current_version": 4,
    "first_observed_at": "2026-05-13T..." }
]
```

Drift flags are written by the foundry's binding-validator job that runs on every card edit. They do not block consumers; they signal that a re-extraction is needed. The next foundry pipeline run resolves them or escalates to human review on conflict.

### 5.4 Extraction provenance

`extraction_provenance` records *how* the bindings were derived:
- `llm_from_card_body:<model_id>` — primary path; an LLM parsed the card body.
- `human_authored` — bindings hand-written, no LLM involvement.
- `migrated:<source>` — imported from a prior format.

`human_reviewed: true` indicates a human has signed off on the LLM extraction. Unreviewed extractions remain valid for use but the publisher subspec (`bundle_publishers_spec.md`) may opt to exclude them from production publishing by default.

---

## 6. `governance.json` — operational state

```json
{
  "governance_version": "1.0",
  "rendered_at": "2026-05-15T...",
  "asset_rk": "...",

  "owners": [
    { "user": "jane.k@acme.com",
      "role": "business_owner",
      "source": "table_owner",
      "since": "2025-11-01" },
    { "user": "data-platform@acme.com",
      "role": "technical_owner",
      "source": "table_owner",
      "since": "2025-11-01" }
  ],

  "followers": [ ... ],

  "effective_sensitivity_class": "confidential",
  "sensitivity_inheritance": [
    { "tier": "T1", "value": "confidential" },
    { "tier": "T2", "value": null },
    { "tier": "T3", "value": null },
    { "tier": "T4", "value": null }
  ],

  "effective_pii_categories": ["names", "contact", "health"],
  "pii_inheritance": [ ... ],

  "tags": [
    { "name": "Clinical", "source": "schema_ext.domain_tags" },
    { "name": "HIPAA-covered", "source": "schema_ext.domain_tags" }
  ],

  "badges": [ ... ],

  "usage_summary": {
    "read_count_30d": 4128,
    "distinct_readers_30d": 23,
    "last_used_at": "2026-05-14T..."
  },

  "lineage": {
    "upstream": [
      { "from_rk": "...", "from_kind": "table",
        "edge_kind": "derived_from",
        "evidence_kind": "extracted_dbt",
        "evidence_ref": "models/clinical/encounters.sql",
        "confidence": 0.99 }
    ],
    "downstream": [
      { "to_rk": "...", "to_kind": "table",
        "edge_kind": "replicated_to",
        "evidence_kind": "observed_dag",
        "pipeline_ref": "fivetran-snowflake-bigquery-mirror" }
    ]
  }
}
```

`effective_sensitivity_class` comes from `v_asset_effective` (T2–T6 spec §13). The `sensitivity_inheritance[]` array is the chain shown for transparency, useful when a downstream skill needs to explain *why* an asset has its current sensitivity (e.g., for compliance documentation).

---

## 7. `causal.json` — claims and candidates this asset participates in

```json
{
  "causal_version": "1.0",
  "rendered_at": "2026-05-15T...",
  "asset_rk": "...",

  "claims": [
    {
      "claim_id": "claim-7a3b...",
      "claim_type": "causal",                /* matches ClaimType in ontology_foundry.models */
      "subject_ref": "Employee.training_completion_rate",
      "predicate":   "leading_indicator_of",
      "object_ref":  "compliance_gap",
      "evidence": [
        { "kind": "chunk",   "ref": "doc:hr-policy-2024#chunk-12" },
        { "kind": "sql_pair", "ref": "sql_pair:csod_risk_attrition#q42" }
      ],
      "confidence": 0.78,
      "extracted_by": "ontology_foundry.relations:SeededLlmRelationStage",
      "extracted_at": "2026-04-22T..."
    }
  ],

  "candidates": [
    {
      "candidate_id": "cand-9f1c...",
      "subject_ref": "Employee.department_id",
      "predicate":   "moderates_effect_of",
      "object_ref":  "overdue_risk",
      "mechanism_hint": "department-specific compliance culture",
      "evidence": [
        { "kind": "correlation_finding",
          "ref": "correlation:dept_id<>overdue_rate#bootstrap_ci=[0.18,0.31]" }
      ],
      "confidence": 0.42,
      "status": "proposed",                  /* 'proposed'|'validated'|'rejected'|'promoted_to_claim' */
      "validation_method": null,
      "extracted_by": "ontology_foundry.causal:edge_consensus"
    }
  ],

  "causal_node_participation": [
    { "card_id": "overdue_risk",     "role": "subject" },
    { "card_id": "compliance_gap",   "role": "subject" }
  ]
}
```

The `claims[]` shape mirrors `ontology_foundry.models.ClaimArtifact` (`claim_type ∈ {DEFINITION, RULE, CAUSAL, GOVERNANCE}`, `chunk_id`, `confidence`, `entity_refs`, `source`). The bundle uses the same shape; serialization is direct.

The `candidates[]` shape carries causal candidates from the existing causal pipeline (`ontology_foundry.causal.edge_consensus`, `ontology_foundry.analysis.CandidatePairArtifact`). Status `promoted_to_claim` indicates a candidate that passed refutation tests and human review.

---

## 8. `metrics.json` — generated index

```json
{
  "metrics_version": "1.0",
  "rendered_at": "...",
  "asset_rk": "...",
  "primary": [
    { "metric_rk": "metric://acme-snowflake-prod/finance_marts/revenue_usd",
      "name": "revenue_usd",
      "definition_kind": "sql_aggregation",
      "grain": "transaction",
      "default_time_grain": "day" }
  ],
  "referenced_in": [
    { "metric_rk": "metric://acme-snowflake-prod/finance_marts/customer_ltv",
      "name": "customer_ltv",
      "role": "dimension_join" }
  ]
}
```

Generated from the inverse of every metric's `metric_reference.depends_on[]` and `primary_asset`. Authors never write this file directly.

`role` values for `referenced_in[]`:
- `dimension_join` — joined for a dimension.
- `fact_input` — contributes a fact value (only for metrics whose primary is elsewhere).
- `filter_only` — used for filtering, not value contribution.

---

## 9. Catalog rollup files

### 9.1 `catalog.json`

A projection of T2 + the T0/T1 chain above it. Stable; changes only when the catalog itself is edited.

```json
{
  "catalog_version": "1.0",
  "rendered_at": "...",
  "catalog_uid": "acme-snowflake-prod::catalog::analytics",
  "catalog_name": "analytics",
  "source_id": "acme-snowflake-prod",
  "org_id": "acme-corp",
  "display_name": "Analytics",
  "description": "...",
  "purpose": "Curated marts for clinical, finance, and workforce.",
  "lifecycle_stage": "production",
  "access_pattern": "read_only",
  "business_owner": "Data Platform Team",
  "managed_by": "dbt",
  "dbt_project_ref": "github.com/acme/analytics-dbt",
  "sensitivity_class": null,                 /* inherits from source */
  "default_refresh_cadence": null,
  "default_freshness_sla": null
}
```

### 9.2 `catalog_assets_index.json`

Generated. Lists every asset rk in this catalog, grouped by kind. Regenerated whenever an asset is added, removed, or renamed.

```json
{
  "index_version": "1.0",
  "rendered_at": "...",
  "catalog_uid": "acme-snowflake-prod::catalog::analytics",
  "tables": [
    { "rk": "...", "name": "encounters",   "schema": "clinical_marts" },
    { "rk": "...", "name": "patients",     "schema": "clinical_marts" }
  ],
  "views": [
    { "rk": "...", "name": "v_active_patients", "schema": "clinical_marts" }
  ],
  "materialized_views": [ ... ],
  "functions": [
    { "rk": "...", "name": "fn_normalize_phone", "schema": "utils" }
  ],
  "metrics": [
    { "rk": "...", "name": "revenue_usd", "schema": "finance_marts" }
  ]
}
```

---

## 10. Bundle versioning

### 10.1 Per-file versions

Each bundle file carries `<concern>_version` in its top-level object. Bumped on schema-breaking changes only. Within a major (e.g., `1.x`), readers must accept additive fields and ignore unknown ones.

### 10.2 Atomic regeneration

The bundle directory is rewritten atomically per asset on regeneration: write to a sibling temp directory, fsync, rename. This guarantees consumers never see a half-written bundle.

### 10.3 Manifest

Each asset bundle gets a `bundle_manifest.json`:

```json
{
  "bundle_manifest_version": "1.0",
  "asset_rk": "...",
  "asset_kind": "table",
  "files": [
    { "name": "mdl.json",                "size": 8421, "sha256": "...", "concern_version": "2.0" },
    { "name": "context.json",            "size": 1287, "sha256": "...", "concern_version": "1.0" },
    { "name": "semantic_bindings.json",  "size": 2103, "sha256": "...", "concern_version": "1.0" },
    { "name": "governance.json",         "size": 3187, "sha256": "...", "concern_version": "1.0" },
    { "name": "causal.json",             "size": 5621, "sha256": "...", "concern_version": "1.0" },
    { "name": "metrics.json",            "size":  423, "sha256": "...", "concern_version": "1.0" }
  ],
  "rendered_at": "2026-05-15T...",
  "renderer_version": "ontology-foundry==X.Y.Z"
}
```

Publishers and consumers check the manifest to detect drift between expected and actual bundle contents.

---

## 11. Generation triggers (forward reference)

The persistence + ingestion spec defines triggers, but for orientation:

| File | Regenerated when |
|---|---|
| `mdl.json` | `table_metadata` / `column_metadata` / `api_endpoint_metadata` / our `_ext` tables / amundsenrds descriptions change |
| `context.json` | `organization` / `source` / `catalog` / `schema_ext` change |
| `semantic_bindings.json` | Card edit affecting bound cards; binding-validator run |
| `governance.json` | Owner / follower / usage / sensitivity / `lineage_edge` change |
| `causal.json` | Foundry pipeline emits new `ClaimArtifact` / candidate referencing this asset |
| `metrics.json` | `metric_metadata` change where this asset is `primary_asset` or `depends_on` |
| `catalog_assets_index.json` | Any asset added / removed / renamed in the catalog |
| `bundle_manifest.json` | Any of the above |

---

## 12. Relationship to existing artifacts

### 12.1 Legacy `genieml/data/sql_meta/*` MDLs

These files become **v1 MDLs** under the new versioning. They are upgrade-compatible: `mdl_version: "1.0"` is implied for files without a version field; emitter raises to `2.0` on next regeneration. No migration sweep is required for read paths.

### 12.2 `project_metadata.json` (genieml)

Splits per the prior analysis: per-project metadata becomes a `data_product` descriptor referencing tuples of `(source_id, catalog_name, schema_name, asset_name)`. Out of scope here; covered in the deferred data-product spec. The `data_product_member text[]` field on `table_ext` (T2–T6 spec §6.1) is the bridge for now.

### 12.3 `TabularContextBundle`

Constructed per-emission from storage via `bundle_from_storage(asset_rk=..., store=...)`. Its `columns[]` profiles feed the `mdl.json` columns' `properties` block (existing `description`, plus extended fields like `is_pii` from `column_ext`). Frequencies and stats remain in the bundle's downstream consumers (causal analysis) but the durable view is in amundsenrds `column_stat`.

### 12.4 `ClaimArtifact` / `RelationArtifact`

Direct re-serialization. The bundle's `causal.json.claims[]` is a sequence of `ClaimArtifact` Pydantic instances dumped to JSON. The publisher subspec can choose to flatten or restructure; the bundle keeps the foundry shape.

---

## 13. Examples

### 13.1 Minimal tabular asset

```
tenants/acme-corp/assets/acme-snowflake-prod/clinical_marts/encounters/
  mdl.json                    -- one model entry, columns array
  context.json                -- T0/T1/T2/T3 chain
  semantic_bindings.json      -- bound to 'encounter' object_type card
  governance.json             -- owner: clinical-data@acme.com, sensitivity: restricted (PHI)
  causal.json                 -- subject in 'overdue_risk' and 'compliance_gap' nodes via Patient → Encounter
  metrics.json                -- primary for 'avg_encounter_duration_minutes'
  bundle_manifest.json
```

### 13.2 API endpoint

```
tenants/acme-corp/assets/acme-salesforce/standard_objects/Account/
  mdl.json                    -- one endpoint entry, fields array
  context.json
  semantic_bindings.json      -- bound to 'customer' object_type card
  governance.json             -- owner: sales-ops@acme.com, sensitivity: confidential
  causal.json                 -- subject in 'churn_risk' node
  metrics.json                -- referenced in 'revenue_usd', 'customer_ltv'
  bundle_manifest.json
```

### 13.3 Metric asset

```
tenants/acme-corp/assets/acme-snowflake-prod/finance_marts/revenue_usd/
  mdl.json                    -- one metric entry
  context.json
  semantic_bindings.json      -- bound to 'monetary_amount' object_type + 'revenue' metric card
  governance.json             -- owner: finance-bi@acme.com
  causal.json                 -- predicted by 'pipeline_strength' causal node
  metrics.json                -- primary entry refers to itself; referenced_in empty
  bundle_manifest.json
```

---

## 14. Open items

- **Bundle compression / archival** — for large tenants the bundle tree may grow. A future spec may define `.bundle.tar.zst` archives for catalog-level snapshots. Defer.
- **Localization** — when an org carries `supported_languages`, are descriptions stored as one-per-language? Currently single-language per bundle; revisit when first multilingual tenant onboards.
- **External evidence references** — `causal.json` evidence entries reference internal artifacts (chunks, sql_pairs). External evidence (regulatory documents, market reports) likely needs a `kind: external_url` evidence type. Defer.
- **Bundle versioning policy** — minor version bumps additive-only; major bumps require migration utility. The migration utility itself is not yet specced.

---

## 15. Change log

| Date | Change |
|---|---|
| 2026-05-15 | Initial draft. |
