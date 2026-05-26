# T0 / T1 Addendum — amundsenrds Linkage

**Status:** Addendum 2026-05-15.
**Amends:** `T0_T1_organization_source_spec.md` (locked 2026-05-15).
**Reason:** When T0/T1 were locked, the downstream `T2_to_T6_amundsenrds_sidecar_spec.md` had not yet committed to using amundsenrds for the tabular spine. With that commitment now made and persistence pipelines specified, T1 needs an explicit linkage field to amundsenrds `cluster_metadata`. This addendum specifies that linkage without otherwise reopening the T0/T1 lock.

---

## 1. Scope of amendment

Two surfaces are touched:

1. **`source` table** — adds a `cluster_rk` column referencing `cluster_metadata.rk`.
2. **Bootstrap procedure** — registering a new Source now creates (or links to) a `database_metadata` row + a `cluster_metadata` row in the same transaction.

Plus two clarifications that downstream specs depend on:

3. The mapping from T1 `kind` enum to amundsenrds `database_metadata.name`.
4. The behavior when `kind` is API-shaped (no amundsenrds Cluster involvement at T4 emission time).

Nothing else in the T0/T1 spec changes. The YAML wire format, the operating-region semantics, the entity-authority declaration model, and the residency posture are unchanged.

---

## 2. `source` table — added column

```sql
ALTER TABLE source
  ADD COLUMN cluster_rk text REFERENCES cluster_metadata(rk) ON DELETE RESTRICT;

CREATE INDEX idx_source_cluster_rk ON source (cluster_rk);
```

### 2.1 Nullability and population

`cluster_rk` is **nullable** because Source records can be created in the YAML import path before any ingest has run (and therefore before any amundsenrds rows exist for the corresponding cluster). The bootstrap procedure (§4) populates it on first ingest or via an explicit `link_source_to_cluster()` operation.

### 2.2 `ON DELETE RESTRICT`

When amundsenrds upstream cascades a delete of a cluster row, the link to a Source must be removed first by the operator. This prevents accidental orphaning of T1 metadata. Operators run `unlink_source_from_cluster()` before deleting upstream rows.

### 2.3 One-to-one constraint

A `(source_id, cluster_rk)` pair must be unique:

```sql
CREATE UNIQUE INDEX idx_source_cluster_unique ON source (cluster_rk) WHERE cluster_rk IS NOT NULL;
```

This enforces that a single amundsenrds Cluster maps to at most one Source. The reverse (a Source maps to at most one Cluster) is enforced by the column being scalar.

---

## 3. Mapping `kind` to amundsenrds `database_metadata.name`

amundsenrds' `database_metadata` row identifies the platform type (the URN's first segment). Our T1 `kind` enum maps onto these names directly:

| T1 `kind` | amundsenrds `database_metadata.name` (rk) |
|---|---|
| `snowflake` | `snowflake` |
| `bigquery` | `bigquery` |
| `redshift` | `redshift` |
| `databricks` | `databricks` |
| `postgres` | `postgres` |
| `mysql` | `mysql` |
| `sqlserver` | `mssql` |
| `oracle` | `oracle` |
| `mongodb` | `mongodb` |
| `s3_parquet` | `s3` |
| `s3_csv` | `s3` |
| `gcs_parquet` | `gcs` |
| `csv_bundle` | `local_file` |
| `salesforce` | *(no amundsenrds row; API-shaped, see §4)* |
| `servicenow` | *(API-shaped)* |
| `workday` | *(API-shaped)* |
| `sap` | *(API-shaped)* |
| `netsuite` | *(API-shaped)* |
| `hubspot` | *(API-shaped)* |
| `marketo` | *(API-shaped)* |
| `zendesk` | *(API-shaped)* |
| `jira` | *(API-shaped)* |
| `github` | *(API-shaped)* |
| `api_feed` | *(API-shaped)* |
| `dbt_project` | *(not a runtime source; metadata-only — see §5)* |
| `looker` | *(BI tool — see §5)* |
| `tableau` | *(BI tool — see §5)* |

A small fixed seed of `database_metadata` rows is inserted during bootstrap (see `hierarchy_persistence_and_ingestion_spec.md` §17 step 4).

---

## 4. API-shaped sources

When `source.kind ∈ {salesforce, servicenow, workday, sap, netsuite, hubspot, marketo, zendesk, jira, github, api_feed}`:

- **No `database_metadata` row** is created. amundsenrds' `database_metadata` is defined for warehouse/lake platforms; API platforms don't fit its model.
- **No `cluster_metadata` row** is created. The Source itself plays the role amundsenrds Cluster plays for warehouses.
- **`source.cluster_rk` remains `NULL`.**
- **`schema_metadata` rows are still created**, but their `cluster_rk` references a per-Source synthetic cluster row in a dedicated `synthetic_clusters` table:

```sql
CREATE TABLE synthetic_cluster (
  cluster_rk    text PRIMARY KEY,                    -- synthetic://{source_id}
  source_id     text NOT NULL REFERENCES source(source_id) ON DELETE CASCADE
);
```

This sidecar exists to satisfy the FK chain `schema_metadata.cluster_rk → cluster_metadata.rk` for API sources — by inserting a `cluster_metadata` row whose `rk` matches the `synthetic_cluster.cluster_rk` pattern, the spine FK chain works uniformly for API and tabular sources alike.

The synthetic `cluster_metadata.rk` format is `synthetic://{source_id}`; downstream consumers can detect API sources by this prefix without joining additional tables.

T4 emission for API sources writes to `api_endpoint_metadata`, not `table_metadata` (per T2–T6 spec §6.2). The MDL emitter detects this from the Source's `kind` and populates `endpoints[]` rather than `models[]`.

---

## 5. dbt / Looker / Tableau — metadata-only sources

These don't have a runtime data-source URN in the warehouse sense; they provide *metadata* (model definitions, dashboard definitions) that overlays other sources. Handling:

- **dbt projects** are treated as a metadata layer over an underlying warehouse Source. The dbt extractor enriches existing `table_metadata` rows (for dbt models materialized as tables/views in the warehouse) and emits `metric_metadata` rows for the semantic layer. No separate Source-scoped cluster row is needed; the dbt project's `source_id` (`acme-dbt-analytics`) is a Source primarily for ownership/governance purposes and has `cluster_rk NULL` and no synthetic cluster.
- **Looker / Tableau** are similar — they consume warehouses, define metrics + dashboards, and the foundry extracts that. Treated as Sources with `cluster_rk NULL` and no synthetic cluster.

Dashboards from BI tools are not modeled in `mdl_bundle_spec.md` v2 (no Dashboard asset_kind yet). Deferred; the asset emergence rules above only cover existing asset_kinds.

---

## 6. Bootstrap procedure additions

The T0/T1 spec's bootstrap workflow now includes the amundsenrds linkage step. Updated sequence for `create_source(source: SourceIn)`:

```
1. Validate SourceIn (kind, required fields).
2. Begin Postgres transaction.
3. Insert into source(...). cluster_rk = NULL initially.
4. If kind is tabular (warehouse/database):
     a. Ensure database_metadata row for the kind exists; insert if missing.
     b. Insert cluster_metadata row with rk = '{kind_db}://{instance_name}'.
     c. Update source.cluster_rk to the new cluster_metadata.rk.
5. If kind is API-shaped:
     a. Insert cluster_metadata row with rk = 'synthetic://{source_id}'.
     b. Insert synthetic_cluster mapping.
     c. Leave source.cluster_rk NULL (the synthetic cluster is not a real cluster).
6. Write audit row (action='create', tier='T1', entity_uid={source_id}).
7. Commit.
8. Enqueue Qdrant index for hier_t1_sources.
```

For metadata-only sources (dbt/Looker/Tableau), neither (4) nor (5) applies; `cluster_rk` stays NULL with no synthetic cluster.

---

## 7. Source query patterns

Consumers asking "what cluster does this source map to" should:

```python
# Direct attribute on source:
src = store.get_source(source_id)
if src.cluster_rk is not None:
    # Tabular source — cluster_rk is a real amundsenrds Cluster
    cluster = store.get_cluster(src.cluster_rk)
else:
    # API or metadata-only source — no direct cluster
    # But api_endpoint_metadata.schema_rk → schema_metadata.cluster_rk = 'synthetic://...'
    # if needed for spine traversal
    pass
```

Cross-cutting query (find all schemas in a Source, regardless of subtype):

```sql
SELECT s.rk, s.name FROM schema_metadata s
JOIN cluster_metadata c ON c.rk = s.cluster_rk
JOIN source src ON
   (c.rk = src.cluster_rk) OR
   (c.rk = 'synthetic://' || src.source_id)
WHERE src.source_id = $1;
```

A view encapsulates this:

```sql
CREATE VIEW v_source_schemas AS
SELECT src.source_id, s.rk AS schema_rk, s.name AS schema_name,
       CASE WHEN c.rk LIKE 'synthetic://%' THEN 'api' ELSE 'tabular' END AS source_subtype
FROM source src
LEFT JOIN cluster_metadata c
  ON c.rk = src.cluster_rk OR c.rk = 'synthetic://' || src.source_id
LEFT JOIN schema_metadata s ON s.cluster_rk = c.rk;
```

---

## 8. Effect on `entities_of_record`

Unchanged. The entity-authority declarations from §3.3 of the original T0/T1 spec stand whether the Source is tabular or API-shaped. A Salesforce Source declaring `entities_of_record: [Customer]` is exactly as meaningful as a Workday Source declaring `entities_of_record: [Employee]`.

The mapping from declared entities to actual MDL-asset bindings happens at the bindings layer (per `mdl_bundle_spec.md` §5) — there a Salesforce endpoint with `binds_to.primary_object_type=customer` realizes the source-level declaration.

---

## 9. Effect on `declared_residency`

Unchanged. Residency posture is decoupled from the storage substrate. Whether the source produces amundsenrds Cluster rows or synthetic cluster rows has no bearing on residency declaration.

---

## 10. Migration notes

For deployments that loaded T0/T1 records before this addendum:

1. Run a one-shot migration that, for each existing `source` row:
   - If `kind` is tabular: try to match the `instance_name` against existing `cluster_metadata.rk` values; on match, set `source.cluster_rk`. On no match, log for operator review (likely needs an ingest run before linkage is possible).
   - If `kind` is API-shaped: insert the `synthetic_cluster` and `cluster_metadata` rows as in §6.5.
2. Re-emit affected bundles to refresh `context.json` with the cluster linkage (consumers should not see staleness, but the bundle representation evolves slightly).

Operator-facing migration utility: `python -m ontology_foundry.migrate.t1_amundsenrds_linkage`.

---

## 11. Tests added

| Test | Coverage |
|---|---|
| `test_create_source_tabular_creates_cluster` | Bootstrap §6 step 4 |
| `test_create_source_api_creates_synthetic` | Bootstrap §6 step 5 |
| `test_create_source_metadata_only_creates_neither` | dbt/Looker case (§5) |
| `test_unique_source_per_cluster` | §2.3 constraint |
| `test_v_source_schemas_covers_both_subtypes` | §7 view correctness |
| `test_migration_links_existing_sources` | §10 migration idempotency |

---

## 12. Open items

- **Multiple Sources per real Cluster** — Currently disallowed by §2.3. Some orgs run multiple logical Sources against the same physical Snowflake account (different roles/warehouses). If that becomes a common pattern, relax the unique constraint and add a `source.cluster_role` discriminator.
- **Dashboard asset_kind** — When BI dashboards become a first-class asset, this addendum needs updating to specify how Looker/Tableau Sources emit dashboard rows.
- **Pack-shipped `database_metadata` rows** — Currently created during bootstrap per §3. If the platform pack distribution model wants to ship the seed `database_metadata` set, the pack onboarding step should insert them. Coordinate with `causal_ontology_foundry_design.md` pack model.

---

## 13. Change log

| Date | Change |
|---|---|
| 2026-05-15 | Initial addendum. |
