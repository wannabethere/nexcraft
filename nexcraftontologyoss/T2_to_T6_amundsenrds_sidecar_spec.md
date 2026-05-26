# T2–T6 — Storage Specification (amundsenrds + Sidecars)

**Status:** Draft 2026-05-15.
**Part of:** Data Knowledge Hierarchy series.
**Depends on:** `T0_T1_organization_source_spec.md`, `semantic_layer_card_spec.md`.
**Adopts:** [`amundsenrds`](https://github.com/amundsen-io/amundsenrds/tree/7cbd0521c280ebce3d5c8a8ed0858a52b2ae4c98) at commit `4509bb0`.
**Forward refs:** `mdl_bundle_spec.md`, `hierarchy_persistence_and_ingestion_spec.md`.
**Leverages:** `ontology_foundry.context.TabularContextBundle` (asset profiling consumes our storage; bundle is an emission artifact, not a storage concern).

---

## 1. Scope

This spec defines the storage model for tiers **T2 (Catalog)** through **T6 (Code List / Value)** of the Data Knowledge Hierarchy. It adopts `amundsenrds` for the tabular metadata spine (`database_metadata` → `cluster_metadata` → `schema_metadata` → `table_metadata` → `column_metadata`) and adds:

1. **Sidecar tables** for tier knowledge our system needs that amundsenrds does not model (purpose, lifecycle, domain tags, ontology bindings).
2. **Parallel tables** for asset subtypes amundsenrds does not cover (API endpoints, functions, metrics, streams).
3. **Union views** that unify subtypes for downstream consumers.
4. **Lineage** as a single kind-aware edge table covering all subtypes.

amundsenrds tables are adopted **as-is**. We never modify them; all augmentation is via sidecar tables joined by `rk`.

Out of scope: T0/T1 (covered in `T0_T1_organization_source_spec.md`); cards (covered in `semantic_layer_card_spec.md`); the bundle wire format (covered in `mdl_bundle_spec.md`); ingestion pipelines (covered in `hierarchy_persistence_and_ingestion_spec.md`).

---

## 2. Tier mapping summary

| Tier | Concept | amundsenrds | Our tables |
|---|---|---|---|
| T0 | Organization | — | `organization`, `operating_region` (from T0/T1 spec) |
| T1 | Source | `database_metadata`, `cluster_metadata` | `source` (joins `cluster_metadata.rk`), `entity_authority_claim` |
| **T2** | **Catalog** | **— (none)** | **`catalog`, `schema_catalog`** |
| **T3** | **Schema** | **`schema_metadata`** | **`schema_ext`** |
| **T4** | **Asset (kinds: table / view / materialized_view / api_endpoint / function / metric / stream)** | **`table_metadata`** (tables, views, MVs) | **`table_ext`** + new tables for each non-tabular subtype |
| **T5** | **Field (kinds: column / api_field / function_parameter / metric_dimension)** | **`column_metadata`** | **`column_ext`** + new tables for each non-column subtype |
| **T6** | **Code List / Value** | — | **`code_list`, `code_value`** |

Plus cross-cutting:
- `lineage_edge` — single edge table for all lineage relationships across subtypes.
- `v_asset` / `v_field` / `v_asset_owner` / `v_asset_usage` — union views unifying T4 / T5 subtypes for consumer queries.

---

## 3. `rk` (resource key) conventions

amundsenrds keys every spine row by `rk`, a stable URN-style string. We extend the convention to cover non-tabular subtypes.

### 3.1 amundsenrds-native (tabular)

For tables/views/materialized views, `rk` follows amundsenrds extractor convention. Typical form:

```
{database}://{cluster}.{schema}/{table}
```

Example: `snowflake://acme-prod.analytics.clinical_marts/encounters`.

Columns: `{table_rk}/{column}`.

### 3.2 Our subtypes

| Subtype | `rk` format |
|---|---|
| API endpoint | `api://{source_id}/{schema_name}/{endpoint_name}` |
| API field | `{endpoint_rk}/{field_path}` (use dotted path for nested fields) |
| Function | `function://{source_id}/{schema_name}/{function_name}({signature_hash})` (signature_hash disambiguates overloads) |
| Function parameter | `{function_rk}#{ordinal}` |
| Metric | `metric://{source_id}/{schema_name}/{metric_name}` |
| Metric dimension | `{metric_rk}#{dimension_name}` |
| Stream | `stream://{source_id}/{schema_name}/{stream_name}` |
| Code list | `codelist://{table_rk_or_endpoint_rk}/{field_name}` |
| Code value | `{code_list_rk}#{value}` |

Catalog `rk` is **derived**, not stored on `schema_metadata`. We compute it as `{source_id}::catalog::{catalog_name}` and store it in `catalog.rk` (see §4).

### 3.3 Determinism

`rk` is deterministic from identity components. The same source + namespace path + name always produces the same `rk`. Renames produce *new* `rk`s; the old `rk` is retained for lineage history with `lifecycle_stage = removed`.

---

## 4. T2 — Catalog

amundsenrds has no Catalog tier (its spine jumps from Cluster to Schema). We add it as a sibling table joined to Schema via a sidecar mapping. This avoids forking amundsenrds.

### 4.1 `catalog` table

```sql
CREATE TABLE catalog (
  catalog_uid              text PRIMARY KEY,         -- {source_id}::catalog::{catalog_name}
  source_id                text NOT NULL REFERENCES source(source_id) ON DELETE CASCADE,
  catalog_name             text NOT NULL,
  display_name             text NOT NULL,
  description              text,
  purpose                  text,
  lifecycle_stage          text NOT NULL,             -- production|development|deprecated|archived|removed
  access_pattern           text NOT NULL,             -- read_only|read_write|landing|sandbox
  business_owner           text,
  technical_owner          text,
  sensitivity_class        text,                      -- inherits from source if NULL
  pii_categories           text[],                    -- inherits from source if NULL
  default_refresh_cadence  jsonb,                     -- overrides source default
  default_freshness_sla    interval,                  -- overrides source default
  managed_by               text,                      -- dbt|terraform|manual|vendor
  dbt_project_ref          text,
  notes                    text,
  created_at               timestamptz NOT NULL DEFAULT now(),
  updated_at               timestamptz NOT NULL DEFAULT now(),
  UNIQUE (source_id, catalog_name)
);

CREATE INDEX idx_catalog_source ON catalog (source_id);
CREATE INDEX idx_catalog_lifecycle ON catalog (lifecycle_stage);
```

### 4.2 `schema_catalog` sidecar

Joins amundsenrds `schema_metadata.rk` to our `catalog.catalog_uid`. Many schemas to one catalog.

```sql
CREATE TABLE schema_catalog (
  schema_rk     text PRIMARY KEY REFERENCES schema_metadata(rk) ON DELETE CASCADE,
  catalog_uid  text NOT NULL REFERENCES catalog(catalog_uid) ON DELETE RESTRICT
);

CREATE INDEX idx_schema_catalog_by_catalog ON schema_catalog (catalog_uid);
```

ON DELETE RESTRICT prevents losing the catalog binding when amundsenrds upstream-deletes a schema; the schema_catalog row is removed first by the ingestion pipeline.

### 4.3 Catalog identity vs schema identity

A schema's amundsenrds `rk` does not embed the catalog name. For sources where the vendor URN naturally includes a "database" segment (e.g., Snowflake `snowflake://account.<database>.<schema>/...`), the extractor's `cluster` field carries the account, and the database segment is parsed into the catalog at the extension layer. The amundsenrds `rk` stays whatever the upstream extractor emits; we map it to our catalog via `schema_catalog`.

---

## 5. T3 — Schema

amundsenrds `schema_metadata` is used as-is. Our `schema_ext` adds knowledge fields.

### 5.1 `schema_metadata` (amundsenrds — unchanged)

Per amundsenrds (commit 4509bb0): `rk` PK, `cluster_rk` FK, `name`, plus children `schema_description`, `schema_programmatic_description`.

### 5.2 `schema_ext` sidecar

```sql
CREATE TABLE schema_ext (
  schema_rk         text PRIMARY KEY REFERENCES schema_metadata(rk) ON DELETE CASCADE,
  display_name      text NOT NULL,
  purpose           text,
  domain_tags       text[] NOT NULL DEFAULT '{}',    -- ['HR', 'Finance', ...] — see §11
  lifecycle_stage   text NOT NULL,                   -- production|development|deprecated|archived|removed
  business_owner    text,
  technical_owner   text,
  sensitivity_class text,                            -- inherits from catalog if NULL
  pii_categories    text[],                          -- inherits from catalog if NULL
  managed_by        text,                            -- overrides catalog default if set
  notes             text,
  created_at        timestamptz NOT NULL DEFAULT now(),
  updated_at        timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX idx_schema_ext_lifecycle ON schema_ext (lifecycle_stage);
CREATE INDEX idx_schema_ext_domain_tags ON schema_ext USING gin (domain_tags);
```

GIN index on `domain_tags` makes "all schemas tagged Finance" cheap.

### 5.3 Inheritance

`sensitivity_class`, `pii_categories`, `default_refresh_cadence`, `default_freshness_sla` flow:

```
source (T1) → catalog (T2) → schema_ext (T3) → table_ext / api_endpoint_ext (T4) → column_ext / api_field_ext (T5)
```

Each level's field is `NULL` to inherit; non-`NULL` overrides. Resolution view in §13.

---

## 6. T4 — Assets

Five subtypes today (plus a stub for `stream` deferred to a later spec).

### 6.1 Table / View / Materialized View

Use amundsenrds `table_metadata` (commit 4509bb0) directly. Carries:
- `rk`, `schema_rk`, `name`, `is_view` (boolean), descriptive children (`table_description`, `table_programmatic_description`), badge/owner/follower/usage tables.

Our extension:

```sql
CREATE TABLE table_ext (
  table_rk             text PRIMARY KEY REFERENCES table_metadata(rk) ON DELETE CASCADE,
  display_name         text NOT NULL,
  purpose              text,
  lifecycle_stage      text NOT NULL,
  is_materialized      boolean NOT NULL DEFAULT false,  -- true for materialized views
  materialization_kind text,                            -- 'view'|'mv'|'mv_incremental'|'mv_scheduled'
  view_definition      text,                            -- DDL for views/MVs
  view_depends_on      text[] NOT NULL DEFAULT '{}',    -- list of upstream table_rk (also drives lineage_edge)
  sensitivity_class    text,
  pii_categories       text[],
  data_product_member  text[] NOT NULL DEFAULT '{}',    -- optional data-product membership tags (see §15)
  notes                text,
  created_at           timestamptz NOT NULL DEFAULT now(),
  updated_at           timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX idx_table_ext_materialized ON table_ext (is_materialized) WHERE is_materialized;
```

When `table_metadata.is_view = true` and `table_ext.is_materialized = true`, the asset is a materialized view.

### 6.2 API Endpoint

```sql
CREATE TABLE api_endpoint_metadata (
  rk                   text PRIMARY KEY,
  schema_rk            text NOT NULL REFERENCES schema_metadata(rk) ON DELETE CASCADE,
  name                 text NOT NULL,
  base_path            text NOT NULL,
  url_template         text,
  http_methods         text[] NOT NULL DEFAULT '{}',     -- ['GET','POST','PATCH','DELETE']
  pagination_kind      text,                              -- 'cursor'|'page'|'offset'|'none'
  auth_scopes          text[] NOT NULL DEFAULT '{}',
  rate_limit_info      jsonb,
  idempotency          boolean,
  request_schema_ref   text,                              -- pointer to OpenAPI fragment if present
  response_schema_ref  text,
  is_deprecated        boolean NOT NULL DEFAULT false,
  updated_at           timestamptz NOT NULL DEFAULT now(),
  UNIQUE (schema_rk, name)
);

CREATE INDEX idx_api_endpoint_schema ON api_endpoint_metadata (schema_rk);

-- Mirror amundsenrds description-with-provenance pattern
CREATE TABLE api_endpoint_description (
  rk           text PRIMARY KEY,
  endpoint_rk  text NOT NULL REFERENCES api_endpoint_metadata(rk) ON DELETE CASCADE,
  description  text NOT NULL,
  source       text NOT NULL DEFAULT 'user'
);

CREATE TABLE api_endpoint_programmatic_description (
  rk           text PRIMARY KEY,
  endpoint_rk  text NOT NULL REFERENCES api_endpoint_metadata(rk) ON DELETE CASCADE,
  description  text NOT NULL,
  source       text NOT NULL                           -- 'extractor:salesforce_sobject', 'extractor:openapi', ...
);
```

`api_endpoint_ext` carries the same shape as `table_ext` for the fields that apply (display_name, purpose, lifecycle_stage, sensitivity overrides, data product membership). Skipping duplication of column-by-column here; structurally identical minus the view-specific fields.

### 6.3 Function

```sql
CREATE TABLE function_metadata (
  rk                  text PRIMARY KEY,
  schema_rk           text NOT NULL REFERENCES schema_metadata(rk) ON DELETE CASCADE,
  name                text NOT NULL,
  language            text NOT NULL,                    -- 'sql'|'python'|'javascript'|'java'|...
  is_deterministic    boolean,
  is_table_valued     boolean NOT NULL DEFAULT false,
  return_type         text,
  definition_body     text,                              -- function DDL or body
  is_deprecated       boolean NOT NULL DEFAULT false,
  updated_at          timestamptz NOT NULL DEFAULT now(),
  UNIQUE (schema_rk, name, rk)                          -- rk disambiguates overloads via signature_hash
);

CREATE INDEX idx_function_schema ON function_metadata (schema_rk);

CREATE TABLE function_description (
  rk            text PRIMARY KEY,
  function_rk   text NOT NULL REFERENCES function_metadata(rk) ON DELETE CASCADE,
  description   text NOT NULL,
  source        text NOT NULL DEFAULT 'user'
);

CREATE TABLE function_programmatic_description (
  rk            text PRIMARY KEY,
  function_rk   text NOT NULL REFERENCES function_metadata(rk) ON DELETE CASCADE,
  description   text NOT NULL,
  source        text NOT NULL
);
```

### 6.4 Metric

```sql
CREATE TABLE metric_metadata (
  rk                       text PRIMARY KEY,
  schema_rk                text NOT NULL REFERENCES schema_metadata(rk) ON DELETE CASCADE,
  name                     text NOT NULL,
  definition_kind          text NOT NULL,            -- 'sql_aggregation'|'ratio'|'count'|'derived'
  expression               text NOT NULL,            -- the formula
  primary_asset_rk         text,                     -- the fact this metric measures (nullable for cross-asset metrics)
  primary_asset_kind       text,                     -- 'table'|'view'|'api_endpoint'
  default_time_grain       text,                     -- 'day'|'week'|'month'|'quarter'|'year'|'transaction'
  format                   jsonb,                    -- {kind: 'currency', currency: 'USD'} | {kind: 'percentage'} | ...
  semantic_layer_source    text,                     -- 'dbt_semantic'|'cube'|'lookml'|'authored'
  is_deprecated            boolean NOT NULL DEFAULT false,
  updated_at               timestamptz NOT NULL DEFAULT now(),
  UNIQUE (schema_rk, name)
);

CREATE INDEX idx_metric_schema ON metric_metadata (schema_rk);
CREATE INDEX idx_metric_primary_asset ON metric_metadata (primary_asset_rk, primary_asset_kind);

CREATE TABLE metric_description (...);                 -- same pattern
CREATE TABLE metric_programmatic_description (...);    -- same pattern
```

### 6.5 Stream (stub)

Reserved for a future spec. The MDL `streams[]` slot is open; the table is not yet created. ETA: when first streaming customer onboards.

---

## 7. T5 — Fields

### 7.1 Column

amundsenrds `column_metadata` (commit 4509bb0) used as-is. Carries `rk`, `table_rk`, `name`, `col_type`, `sort_order`, `is_nullable`, plus `column_description`, `column_programmatic_description`, `column_stat`, `column_badge`.

Our extension:

```sql
CREATE TABLE column_ext (
  column_rk           text PRIMARY KEY REFERENCES column_metadata(rk) ON DELETE CASCADE,
  display_name        text,
  purpose             text,
  is_pii              boolean NOT NULL DEFAULT false,
  pii_categories      text[] NOT NULL DEFAULT '{}',     -- empty unless is_pii
  is_business_key     boolean NOT NULL DEFAULT false,
  semantic_unit       text,                              -- 'currency_usd'|'percentage'|'count'|...
  sensitivity_class   text,
  notes               text,
  created_at          timestamptz NOT NULL DEFAULT now(),
  updated_at          timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX idx_column_ext_pii ON column_ext (is_pii) WHERE is_pii;
```

### 7.2 API Field

```sql
CREATE TABLE api_field_metadata (
  rk                       text PRIMARY KEY,
  endpoint_rk              text NOT NULL REFERENCES api_endpoint_metadata(rk) ON DELETE CASCADE,
  name                     text NOT NULL,
  field_path               text NOT NULL,                 -- dotted path for nested
  field_type               text NOT NULL,                 -- 'STRING'|'INTEGER'|'BOOLEAN'|'OBJECT'|'ARRAY'|'REFERENCE'|...
  is_nullable              boolean NOT NULL DEFAULT true,
  in_request               boolean NOT NULL DEFAULT false,
  in_response              boolean NOT NULL DEFAULT false,
  in_query                 boolean NOT NULL DEFAULT false,
  in_path                  boolean NOT NULL DEFAULT false,
  writable                 boolean NOT NULL DEFAULT false,
  readable                 boolean NOT NULL DEFAULT true,
  referenced_endpoint_rk   text REFERENCES api_endpoint_metadata(rk),
  enum_codelist_rk         text REFERENCES code_list(rk),
  is_pii                   boolean NOT NULL DEFAULT false,
  pii_categories           text[] NOT NULL DEFAULT '{}',
  semantic_unit            text,
  updated_at               timestamptz NOT NULL DEFAULT now(),
  UNIQUE (endpoint_rk, field_path)
);

CREATE INDEX idx_api_field_endpoint ON api_field_metadata (endpoint_rk);
CREATE INDEX idx_api_field_referenced ON api_field_metadata (referenced_endpoint_rk);

CREATE TABLE api_field_description (...);              -- same pattern
CREATE TABLE api_field_programmatic_description (...); -- same pattern
```

### 7.3 Function parameter

```sql
CREATE TABLE function_parameter (
  rk             text PRIMARY KEY,                       -- {function_rk}#{ordinal}
  function_rk    text NOT NULL REFERENCES function_metadata(rk) ON DELETE CASCADE,
  ordinal        integer NOT NULL,
  name           text,                                   -- nullable for positional-only params
  param_type     text NOT NULL,
  mode           text NOT NULL DEFAULT 'IN',             -- 'IN'|'OUT'|'INOUT'
  default_value  text,
  UNIQUE (function_rk, ordinal)
);
```

### 7.4 Metric dimension

```sql
CREATE TABLE metric_dimension (
  rk                  text PRIMARY KEY,
  metric_rk           text NOT NULL REFERENCES metric_metadata(rk) ON DELETE CASCADE,
  name                text NOT NULL,
  source_field_rk     text,                              -- the underlying column/api_field rk if direct
  source_field_kind   text,                              -- 'column'|'api_field'
  join_path           text,                              -- when dimension reaches across joins
  description         text,
  UNIQUE (metric_rk, name)
);
```

---

## 8. T6 — Code list / value

Code lists (enums, controlled vocabs) attach to a column or API field. They are first-class because state-transition causal stories depend on them.

```sql
CREATE TABLE code_list (
  rk             text PRIMARY KEY,                       -- codelist://{parent_rk}/{name}
  parent_rk      text NOT NULL,                          -- column_rk OR api_field_rk
  parent_kind    text NOT NULL,                          -- 'column'|'api_field'
  name           text NOT NULL,
  description    text,
  is_closed      boolean NOT NULL DEFAULT true,          -- true if values are fixed (vendor enum)
  source         text NOT NULL DEFAULT 'extracted',      -- 'extracted'|'authored'|'inferred'
  updated_at     timestamptz NOT NULL DEFAULT now(),
  UNIQUE (parent_rk, name)
);

CREATE TABLE code_value (
  rk             text PRIMARY KEY,                       -- {code_list_rk}#{value}
  code_list_rk   text NOT NULL REFERENCES code_list(rk) ON DELETE CASCADE,
  value          text NOT NULL,
  label          text,                                   -- display label
  description    text,
  is_terminal    boolean NOT NULL DEFAULT false,         -- e.g., 'closed' is terminal in a workflow
  ordinal        integer,
  UNIQUE (code_list_rk, value)
);

CREATE INDEX idx_code_value_codelist ON code_value (code_list_rk);
```

A column's `column_ext` may carry a `code_list_rk` pointer (TODO: add column when first ingestion lands; for now resolvable by `parent_rk = column_rk`).

State transitions between code values (e.g., `pending → in_progress → completed`) are not modeled in T6 itself — they live in `derived_state` cards and the causal graph. T6 stores the *vocabulary*; cards describe the *behavior*.

---

## 9. Union views

Downstream consumers (ontology system, publishers, governance) should never need to know which subtype an asset is, except where genuinely necessary. Views unify.

```sql
CREATE VIEW v_asset AS
  SELECT
    t.rk, t.schema_rk, t.name, 'table' AS asset_kind,
    NULL::text[]  AS http_methods, NULL::text AS return_type,
    NULL::text    AS metric_expression
  FROM table_metadata t
  WHERE COALESCE((SELECT is_view FROM table_metadata WHERE rk = t.rk), false) = false
UNION ALL
  SELECT
    t.rk, t.schema_rk, t.name,
    CASE WHEN te.is_materialized THEN 'materialized_view' ELSE 'view' END AS asset_kind,
    NULL, NULL, NULL
  FROM table_metadata t
  LEFT JOIN table_ext te ON te.table_rk = t.rk
  WHERE t.is_view = true
UNION ALL
  SELECT
    rk, schema_rk, name, 'api_endpoint',
    http_methods, NULL, NULL
  FROM api_endpoint_metadata
UNION ALL
  SELECT
    rk, schema_rk, name, 'function',
    NULL, return_type, NULL
  FROM function_metadata
UNION ALL
  SELECT
    rk, schema_rk, name, 'metric',
    NULL, NULL, expression
  FROM metric_metadata;

CREATE VIEW v_field AS
  SELECT
    c.rk, c.table_rk AS parent_rk, c.name, 'column' AS field_kind,
    c.col_type AS field_type
  FROM column_metadata c
UNION ALL
  SELECT
    rk, endpoint_rk AS parent_rk, name, 'api_field',
    field_type
  FROM api_field_metadata
UNION ALL
  SELECT
    rk, function_rk AS parent_rk,
    COALESCE(name, ordinal::text) AS name,
    'function_parameter',
    param_type AS field_type
  FROM function_parameter
UNION ALL
  SELECT
    rk, metric_rk AS parent_rk, name, 'metric_dimension',
    'DIMENSION' AS field_type
  FROM metric_dimension;
```

### 9.1 Governance unification

```sql
CREATE VIEW v_asset_owner AS
  SELECT table_rk AS asset_rk, 'table' AS asset_kind, user_rk, role
  FROM table_owner   -- amundsenrds
UNION ALL
  SELECT endpoint_rk, 'api_endpoint', user_rk, role
  FROM api_endpoint_owner   -- to be created when API governance lands
;

CREATE VIEW v_asset_usage AS
  SELECT table_rk AS asset_rk, 'table' AS asset_kind, user_rk, read_count, last_used_at
  FROM table_usage   -- amundsenrds
UNION ALL
  SELECT endpoint_rk, 'api_endpoint', user_rk, read_count, last_used_at
  FROM api_endpoint_usage
;
```

API-side governance tables follow the same shape as their amundsenrds counterparts; deferring full DDL here to avoid clutter, locked in `hierarchy_persistence_and_ingestion_spec.md`.

---

## 10. Lineage

Single edge table, kind-aware, covers every lineage relationship across subtypes.

```sql
CREATE TABLE lineage_edge (
  edge_id         bigserial PRIMARY KEY,
  from_rk         text NOT NULL,
  from_kind       text NOT NULL,                          -- 'table'|'view'|'api_endpoint'|'function'|'metric'
  to_rk           text NOT NULL,
  to_kind         text NOT NULL,
  edge_kind       text NOT NULL,                          -- see §10.1
  evidence_kind   text NOT NULL,                          -- 'declared_view_ddl'|'declared_metric_deps'|'extracted_dbt'|'observed_dag'|'manual'
  evidence_ref    text,                                   -- pointer into source (DDL hash, dbt model path, DAG id, claim id)
  confidence      real,
  pipeline_ref    text,                                   -- nullable; references a pipeline asset when applicable
  created_at      timestamptz NOT NULL DEFAULT now(),
  active          boolean NOT NULL DEFAULT true,
  UNIQUE (from_rk, from_kind, to_rk, to_kind, edge_kind)
);

CREATE INDEX idx_lineage_from ON lineage_edge (from_rk);
CREATE INDEX idx_lineage_to   ON lineage_edge (to_rk);
CREATE INDEX idx_lineage_edge_kind ON lineage_edge (edge_kind);
```

### 10.1 `edge_kind` vocabulary

| `edge_kind` | Meaning | Direction convention |
|---|---|---|
| `depends_on` | Generic dependency; view → upstream table, metric → its tables, function → its callers' inputs | from = dependent, to = dependency |
| `derived_from` | More specific than depends_on; the dataset is a transformation of another (dbt model materialization) | from = derived, to = source |
| `replicated_from` | One-to-one replication (Fivetran Salesforce → Snowflake table) | from = copy, to = origin |
| `writes_to` | A pipeline asset writes to a data asset | from = pipeline, to = target |
| `reads_from` | A pipeline asset reads from a data asset | from = pipeline, to = source |
| `references` | API field → another endpoint (lookup-style) | from = field's endpoint, to = referenced endpoint |
| `computes` | A function is computed from arguments | from = function, to = arg field |

### 10.2 Auto-population from MDL

When MDL is emitted (per `mdl_bundle_spec.md`):
- `view_definition.depends_on[]` → `lineage_edge` rows with `edge_kind: depends_on`, `evidence_kind: declared_view_ddl`.
- `metric_reference.depends_on[]` → rows with `edge_kind: depends_on`, `evidence_kind: declared_metric_deps`.
- `function_reference.depends_on[]` → rows with `edge_kind: depends_on`, `evidence_kind: declared_function_deps`.

This is the "lineage for free" path: most lineage is declarative in the MDL itself.

### 10.3 Soft-delete on rename

When an asset is renamed (new `rk`), its outbound edges become `active = false` with a `lineage_edge_supersede` audit row; new edges write against the new `rk`. Old edges are retained for historical query support.

---

## 11. Domain tags vocabulary

Schema-level (`schema_ext.domain_tags`) and asset-level (in the bundle, not stored on the asset table directly) controlled vocabulary. Seed set:

```
HR, Finance, Sales, Marketing, Customer_Support, IT_Operations, Security,
Legal, Compliance, Engineering, Product, Clinical, Billing, Supply_Chain,
Operations, Risk, Analytics, Data_Platform
```

Extensible per org via `tenants/<org_id>/domain_tags_vocab.yaml`. The CI gate that validates `domain_tags` against the vocab is enforced at the storage layer.

These tags are orthogonal to the semantic-layer card `kind` taxonomy. Tags describe organizational ownership ("Finance owns this schema"); cards describe semantic identity ("this represents an Employee").

---

## 12. Sensitivity vocabulary

```
public, internal, confidential, restricted
```

Default inheritance: `source → catalog → schema_ext → table_ext / api_endpoint_ext → column_ext / api_field_ext`. Each `NULL` inherits; each non-`NULL` overrides.

PII categories vocabulary:

```
names, contact, government_id, financial, payment, health, biometric, location, employment, behavioral
```

Inheritance applies to `pii_categories` as well: if `schema_ext.pii_categories IS NULL`, inherit from catalog; else use schema's set. (Note: this is *replacement* inheritance, not union — to express "schema adds X to source's set," set the schema field explicitly with the merged list.)

---

## 13. Inheritance resolution view

Materializes the effective values per asset/field so consumers don't walk the chain.

```sql
CREATE VIEW v_asset_effective AS
SELECT
  a.rk            AS asset_rk,
  a.asset_kind,
  sc.catalog_uid,
  s.rk            AS schema_rk,
  src.source_id,
  src.org_id,
  -- effective sensitivity (asset → schema → catalog → source)
  COALESCE(
    CASE WHEN a.asset_kind IN ('table','view','materialized_view') THEN te.sensitivity_class END,
    CASE WHEN a.asset_kind = 'api_endpoint' THEN aee.sensitivity_class END,
    sx.sensitivity_class,
    c.sensitivity_class,
    src.sensitivity_class
  ) AS effective_sensitivity_class,
  -- effective PII categories (replacement inheritance)
  COALESCE(
    CASE WHEN a.asset_kind IN ('table','view','materialized_view') THEN te.pii_categories END,
    CASE WHEN a.asset_kind = 'api_endpoint' THEN aee.pii_categories END,
    sx.pii_categories,
    c.pii_categories,
    src.pii_categories
  ) AS effective_pii_categories,
  -- effective refresh + freshness (asset doesn't override at this level)
  COALESCE(c.default_refresh_cadence, src.refresh_cadence) AS effective_refresh_cadence,
  COALESCE(c.default_freshness_sla,   src.freshness_sla)   AS effective_freshness_sla,
  -- domain tags from schema (already faceted)
  sx.domain_tags
FROM v_asset a
JOIN schema_metadata s   ON s.rk = a.schema_rk
LEFT JOIN schema_ext sx  ON sx.schema_rk = s.rk
LEFT JOIN schema_catalog scj ON scj.schema_rk = s.rk
LEFT JOIN catalog c      ON c.catalog_uid = scj.catalog_uid
JOIN cluster_metadata cl ON cl.rk = s.cluster_rk
JOIN source src          ON src.cluster_rk = cl.rk
LEFT JOIN table_ext te         ON a.asset_kind IN ('table','view','materialized_view') AND te.table_rk = a.rk
LEFT JOIN api_endpoint_ext aee ON a.asset_kind = 'api_endpoint' AND aee.endpoint_rk = a.rk;
```

(`api_endpoint_ext` table not fully shown here for brevity; same shape as `table_ext` minus view-specific fields.)

This is the single view governance consumers should query. It collapses the entire inheritance chain in one SELECT.

---

## 14. Relationship to MDL and `TabularContextBundle`

### 14.1 MDL emission (forward ref to `mdl_bundle_spec.md`)

For each asset, the MDL emitter pulls:
- amundsenrds rows (`schema_metadata`, `table_metadata`, `column_metadata`, descriptions, stats).
- Our extension rows (`table_ext`, `column_ext`, …).
- Catalog and schema chain (`schema_catalog` → `catalog`).
- Source / org chain.
- Effective inherited values from `v_asset_effective`.

Emits `mdl.json`, `context.json`, `governance.json`, etc. Storage is upstream; bundle is downstream artifact.

### 14.2 `TabularContextBundle` reuse

The existing `ontology_foundry.context.TabularContextBundle` becomes a **transient analysis artifact** built on the fly from storage when:
- The causal pipeline needs per-column stats + samples (`correlation_pipeline`, `pairwise_numeric_screen`).
- The card-emitter needs profile context to draft an `object_type` card.

`TabularContextBundle.source_system` populates from the storage source chain. `TabularContextBundle.columns[].stats` populates from `column_stat` (amundsenrds). Storage holds the durable bits; `TabularContextBundle` is constructed per-call.

```python
# ontology_foundry/context/from_storage.py (to be added)

def bundle_from_storage(*, asset_rk: str, store: HierarchyStore) -> TabularContextBundle:
    """Materialize a TabularContextBundle for an asset from durable storage.

    Reuses TabularContextBundle shape; pulls table/column metadata and stats
    from amundsenrds rows and our sidecars rather than from raw frames.
    """
    ...
```

This is the integration seam between the new storage layer and the existing foundry analysis pipelines.

---

## 15. Data products (deferred placeholder)

The `data_product_member text[]` field on `table_ext` (and analogous on API endpoints) is the lightweight tagging hook for data-product membership. A full data-product spec — with explicit `data_product` table, ownership, lifecycle, member assets across sources — is deferred to a sibling spec. The tag-list on `*_ext` is enough to express membership and round-trip through the bundle.

---

## 16. Migration heads

Two Alembic heads:

1. **`amundsenrds`** — upstream's migrations (carried via the pinned commit). Owned by amundsenrds; we run them, never rewrite.
2. **`ours`** — our migrations. Depends on `amundsenrds` head (`down_revision = '<amundsenrds latest>'` at the base of our chain).

Bootstrap order: run amundsenrds migrations, then ours. Future amundsenrds upgrades: re-pin, run new amundsenrds revisions, then our chain re-runs cleanly because `down_revision` references stay valid.

Implementation lives in `hierarchy_persistence_and_ingestion_spec.md`.

---

## 17. Indices and performance notes

- Spine FK chain (`source → cluster → schema → asset`) is fully indexed.
- `domain_tags` GIN index supports faceted filtering.
- `is_pii` partial indexes on `column_ext` / `api_field_ext` keep PII-only queries cheap.
- `lineage_edge` indexed both ways for traversal.
- `v_asset_effective` is a view, not a materialized view. If performance demands later, convert to MV refreshed nightly + on-write triggers.

---

## 18. Open items (deferred)

- **Stream metadata table** — schema TBD; first streaming customer.
- **Per-asset Qdrant payloads** — what subset of `v_asset_effective` columns travel as Qdrant payload filters. Locked in `hierarchy_persistence_and_ingestion_spec.md`.
- **Materialized view refresh schedule modeling** — currently `table_ext.materialization_kind` and storage refresh cadence cover the basics; a richer schedule object may be needed.
- **Data product table** — see §15. Defer.
- **API endpoint extension table column-by-column DDL** — abbreviated in §6.2; lock in the persistence spec.

---

## 19. Change log

| Date | Change |
|---|---|
| 2026-05-15 | Initial draft. |
