# Bundle Publishers — Specification

**Status:** Draft 2026-05-15.
**Part of:** Data Knowledge Hierarchy series.
**Depends on:** `mdl_bundle_spec.md`, `T2_to_T6_amundsenrds_sidecar_spec.md`, `semantic_layer_card_spec.md`, `hierarchy_persistence_and_ingestion_spec.md`.
**Leverages:** `ontology_foundry.llm` (provider abstraction for any LLM-assisted target-format rendering).

---

## 1. Scope

This spec defines the contract by which the foundry **publishes per-asset bundles to external metadata catalogs**, and the per-target mapper implementations. The foundry's role is enriching downstream catalogs with the ontology-anchored knowledge they don't natively have (causal claims, equivalence classes, semantic bindings to canonical entities).

In this version:

| Target | Status | Priority |
|---|---|---|
| Microsoft Purview (Apache Atlas core) | Specified in detail | **1st** |
| Databricks Unity Catalog | Specified in detail | **2nd** |
| DataHub | Stubbed | Deferred |
| OpenMetadata | Stubbed | Deferred |

Each target gets a Mapper that implements the common `BundlePublisher` Protocol.

Out of scope:
- Bundle file shapes (in `mdl_bundle_spec.md`).
- Bundle generation pipeline (in `hierarchy_persistence_and_ingestion_spec.md`).
- Consumer-side query API (in `bundle_consumer_api_spec.md`).

---

## 2. `BundlePublisher` Protocol

```python
class BundlePublisher(Protocol):
    target_name: str
    target_version: str

    def supports(self, bundle: AssetBundle) -> SupportDecision: ...
    """
    Inspect the bundle and decide whether this publisher can publish it.
    Returns:
      - SupportDecision(supported=True)
      - SupportDecision(supported=False, reason="...")
      - SupportDecision(supported='partial', omits=['causal','metrics'], reason="...")
    """

    def publish(self, bundle: AssetBundle, *,
                actor: str,
                dry_run: bool = False) -> PublishResult: ...
    """
    Push the bundle (or its supported subset) to the target.
    Idempotent. Returns mapping of bundle.asset_rk -> target_id, plus a
    structured diff of what changed since last publish.
    """

    def unpublish(self, asset_rk: str, *, actor: str) -> UnpublishResult: ...
    """Remove the asset from the target (or mark deprecated, per target semantics)."""

    def status(self, asset_rk: str) -> PublishStatus: ...
    """
    Inspect the current state in the target for this asset:
      last_published_at, published_manifest_sha256, drift_detected (bool),
      target_id, errors.
    """

    def health(self) -> HealthStatus: ...
    """Connectivity + auth check against the target."""
```

### 2.1 `SupportDecision`

```python
@dataclass
class SupportDecision:
    supported: Literal[True, False, 'partial']
    omits: list[str] = field(default_factory=list)   # e.g., ['causal', 'metrics']
    reason: str = ""
```

`'partial'` indicates the publisher will write what it can but explicitly omit named concerns (e.g., Unity Catalog cannot natively express causal claims; the Unity publisher returns `partial` with `omits=['causal']` and degrades those to long-form descriptions per §5.5).

### 2.2 `PublishResult`

```python
@dataclass
class PublishResult:
    asset_rk: str
    target_id: str
    fidelity: Literal['full', 'partial']
    omitted_concerns: list[str]
    changed_concerns: list[str]                # which of mdl/context/bindings/governance/causal/metrics actually mutated the target
    target_object_kinds_touched: list[str]     # e.g., ['atlas_entity', 'glossary_term', 'relationship']
    diagnostics: dict[str, Any] = field(default_factory=dict)
```

### 2.3 Idempotency

Every publisher MUST be idempotent. Calling `publish` twice with the same bundle and no upstream changes results in zero target mutations. Implementations detect no-op by:
1. Computing `bundle.bundle_manifest.json` sha256.
2. Comparing against the `published_manifest_sha256` stored in the publisher's local sync table.
3. If equal, return early with `changed_concerns=[]`.

### 2.4 Stored publisher state

Each publisher persists per-asset state for idempotency and drift detection:

```sql
CREATE TABLE publisher_state (
  publisher_name           text NOT NULL,
  asset_rk                 text NOT NULL,
  target_id                text NOT NULL,
  last_published_at        timestamptz NOT NULL,
  published_manifest_sha256 text NOT NULL,
  fidelity                 text NOT NULL,
  omitted_concerns         text[] NOT NULL DEFAULT '{}',
  last_error               text,
  PRIMARY KEY (publisher_name, asset_rk)
);
```

---

## 3. Publishing orchestration

### 3.1 When publishing runs

| Trigger | Behavior |
|---|---|
| Operator request: "publish all bundles for source X" | Iterates over assets in source; calls each configured publisher for each |
| Operator request: "publish single asset" | Targeted publish |
| Continuous mode: bundle emission completes | If publisher is configured for continuous mode, enqueue publish task |
| Scheduled (cron) | Iterates all configured publishers; for each, publishes assets whose `bundle_emit_state.emitted_at > publisher_state.last_published_at` |

Default: **manual + scheduled**, not continuous. Bundle regeneration is high-frequency; pushing every change to an external catalog produces noise. Cron-based publish (nightly) gives a stable cadence.

### 3.2 Publisher configuration

Per tenant, in `tenants/<org_id>/publishers.yaml`:

```yaml
publishers:
  - name: acme-purview-prod
    target: purview
    target_version: "2025-04"
    enabled: true
    cadence: "0 5 * * *"        # 5 AM daily
    fidelity_preference: full
    scope:
      include_source_ids: [acme-snowflake-prod, acme-salesforce]
      exclude_lifecycle_stages: [deprecated, archived, removed]
      include_asset_kinds: [table, view, materialized_view, api_endpoint, metric]
    causal_publishing:
      mode: custom_typedef       # 'custom_typedef' | 'glossary_term' | 'long_form_description'
    auth:
      kind: managed_identity
      tenant_id: "..."
      endpoint: "https://acme.purview.azure.com"

  - name: acme-unity-prod
    target: unity_catalog
    enabled: true
    cadence: "0 6 * * *"
    scope:
      include_asset_kinds: [table, view, materialized_view]
    causal_publishing:
      mode: long_form_description
    auth:
      kind: pat
      workspace_url: "https://acme.cloud.databricks.com"
```

Auth references credentials in the secrets backend; this file holds only the references, never secrets.

---

## 4. Microsoft Purview mapper

Purview's metadata model is Apache Atlas. Atlas concepts:
- **Entities** — typed objects with attributes. Type system is extensible (custom typedefs).
- **Relationships** — typed edges between entities (with end-1 / end-2 cardinality).
- **Classifications** — propagating tags (akin to our markings).
- **Glossary** — controlled-vocabulary terms with hierarchies and assignments.
- **Business Metadata** — additional attribute groups attachable to any entity.

### 4.1 Type registration

The Purview publisher registers a small set of custom Atlas typedefs at bootstrap. These are namespaced under `ontology_foundry_*` to avoid collisions.

| Typedef | Atlas category | Purpose |
|---|---|---|
| `ontology_foundry_api_endpoint` | EntityDef | API endpoint asset (Atlas core has no native API endpoint kind) |
| `ontology_foundry_api_field` | EntityDef | API field |
| `ontology_foundry_function` | EntityDef | UDF / stored function |
| `ontology_foundry_metric` | EntityDef | Semantic metric |
| `ontology_foundry_causal_claim` | EntityDef | One causal claim with evidence |
| `ontology_foundry_causal_node` | EntityDef | A causal_node card surface |
| `ontology_foundry_equivalence_class` | EntityDef | A cross-source equivalence group |
| `causal_subject_of` | RelationshipDef | from data_asset → causal_node, end-1 = subject |
| `causal_outcome_of` | RelationshipDef | from causal_node → data_asset, end-2 = outcome |
| `causal_claim_about` | RelationshipDef | from causal_claim → (asset|metric|column) |
| `equivalence_member` | RelationshipDef | from data_asset → equivalence_class |
| `binds_to_canonical_entity` | BusinessMetadataDef | attached to data_asset: card_id, primary_object_type, implements_interfaces |

Registration is idempotent. The publisher's `health()` returns `degraded` if the type registration is missing.

### 4.2 MDL → Atlas entity mapping

| MDL block | Atlas entity type | qualifiedName |
|---|---|---|
| `models[]` with `is_view=false`, not materialized | `snowflake_table` / `azure_synapse_table` / `aws_s3_object` (per source kind) | `{rk}` |
| `models[]` with `is_view=true`, not materialized | `<platform>_view` | `{rk}` |
| `models[]` materialized | `<platform>_table` with `is_materialized=true` business attribute | `{rk}` |
| `endpoints[]` | `ontology_foundry_api_endpoint` | `{rk}` |
| `functions[]` | `ontology_foundry_function` | `{rk}` |
| `metrics[]` | `ontology_foundry_metric` | `{rk}` |
| `columns[]` / `fields[]` | `<platform>_column` / `ontology_foundry_api_field` | `{rk}` |

Atlas qualifiedName uses our `rk` directly — already URN-shaped, deterministic, parseable.

### 4.3 Descriptions

Atlas entity attribute `description` ← MDL `description` (the user-authored or extractor-extracted prose).

For fields, the existing `description` attribute on `<platform>_column` is used; for API fields we use a `description` attribute on `ontology_foundry_api_field`.

Description provenance is captured as a custom attribute `description_provenance` on each entity.

### 4.4 Bindings → Business Metadata + Glossary

For each asset with `semantic_bindings.json`:

1. **Business Metadata** `binds_to_canonical_entity` is attached with attributes:
   - `primary_object_type` (e.g., `employee`)
   - `card_id`
   - `card_version_seen`
   - `implements_interfaces[]`
   - `human_reviewed`
2. **Glossary Term** for the object_type card is created (if not present) under glossary `ontology_foundry_<tenant>`. The asset entity gets a glossary term assignment to that term.
3. **Field bindings** map to per-column Business Metadata `binds_to_card_field` with `card_field` and `binding_kind`.

The card body is rendered into the Atlas glossary term's `longDescription` attribute, giving Purview users human-readable context that mirrors what the LLM sees.

### 4.5 Causal claims and candidates

Configuration `causal_publishing.mode` chooses:

- **`custom_typedef`** (default for Purview): one `ontology_foundry_causal_claim` Atlas entity per claim, with `causal_claim_about` relationships pointing at the subject and object entities. Evidence pointers ride as a JSON-blob attribute.
- **`glossary_term`**: claims aggregated into a per-causal_node glossary term whose `longDescription` lists claims + evidence.
- **`long_form_description`**: claims flattened into the bound entity's `userDescription` attribute. Lossy; only used when typedef registration is unavailable.

Candidates: published when `status='proposed'` AND `confidence >= configured_threshold` (default 0.6). Candidates use the same typedef as claims with attribute `is_candidate=true`. Below threshold candidates are not published.

### 4.6 Markings → Atlas Classifications

Each marking in the bundle's effective markings (from `context.json` + propagated from cards) becomes an Atlas Classification with propagation enabled. Classifications:

| Marking | Atlas Classification |
|---|---|
| `contains_pii` | `PII` (Purview ships this) |
| `contains_phi` | `PHI` (extended classification, registered if missing) |
| `regulated_hipaa` | `HIPAA` |
| `regulated_sox` | `SOX` |
| `regulated_gdpr` | `GDPR` |
| `confidential` | `Confidential` |
| `restricted` | `Restricted` |

Propagation in Atlas already mirrors our semantics (mark a parent → children inherit).

### 4.7 Lineage

`governance.json.lineage.upstream[]` and `downstream[]` → Atlas lineage `Process` entities + `DataSet_Process_Inputs` / `DataSet_Process_Outputs` relationships.

When `evidence_kind='declared_view_ddl'`, the Process entity carries the DDL as an attribute. When `evidence_kind='extracted_dbt'`, the Process entity references the dbt model path.

### 4.8 Equivalence classes

For each equivalence class an asset participates in (`semantic_bindings` lookup): one `ontology_foundry_equivalence_class` entity per class, with `equivalence_member` relationships to every member asset. Class identity is the equivalence_class_id.

### 4.9 Publish sequence

```
1. Authenticate against Purview (token cache; refresh if needed)
2. Verify typedefs are registered (idempotent register if missing)
3. For each asset in scope:
   a. Compute bundle manifest sha256; if matches publisher_state, skip
   b. Resolve target_id (existing Atlas guid by qualifiedName; create if absent)
   c. Upsert the data_asset entity (description, attributes, business metadata)
   d. Upsert classifications (markings)
   e. Upsert binds_to_canonical_entity business metadata
   f. Upsert glossary term for the object_type (per tenant glossary)
   g. Assign glossary term to data_asset
   h. For each column/field: upsert column entity + binds_to_card_field BM
   i. For each causal claim: upsert ontology_foundry_causal_claim entity + relationships
   j. For each lineage edge: upsert Process entities + relationships
   k. For each equivalence membership: upsert equivalence_class entity + relationship
   l. Update publisher_state with new manifest sha
4. Surface metrics + errors
```

### 4.10 Deletion / deprecation

Atlas does not auto-delete on bundle removal. The publisher's `unpublish` sets the entity's `entityStatus = ACTIVE → DELETED` (soft) by default; configurable to hard-delete via Atlas API. Deprecated assets receive a `Deprecated` classification.

---

## 5. Databricks Unity Catalog mapper

Unity Catalog is leaner than Atlas. Its native concepts:
- **Tables / Views / Volumes / Functions** — first-class assets.
- **Tags** — key/value pairs attachable to any object.
- **Comments** — long-form descriptions on tables/columns.
- **Lineage** — system-tracked (read-only via system tables); push-side is limited.
- **AI/BI Semantic Model** — emerging; experimental support for metric registration.
- **Securable bindings** — RBAC-related, not metadata-publishing-relevant.

### 5.1 Asset-kind coverage

| MDL block | Unity object | Notes |
|---|---|---|
| `models[]` table | Unity `Table` | If catalog/schema exist; otherwise skip |
| `models[]` view | Unity `View` | View definition uses `view_definition.query` |
| `models[]` materialized view | Unity `Table` with materialization metadata | Unity has MV support via Delta Live Tables |
| `endpoints[]` API endpoint | **Not natively supported** | Skip with `partial` decision; document via long-form description on a placeholder Table if requested |
| `functions[]` | Unity `Function` | Push DDL via REST or SQL |
| `metrics[]` | AI/BI Semantic Model (if enabled) or skipped | Currently experimental; default skip |

Unity publisher's `supports()` returns `partial` with `omits=['api_endpoint','metric']` when the bundle is for an API endpoint or metric.

### 5.2 Descriptions → comments

- `models[].description` → table `COMMENT`.
- `columns[].properties.description` → column `COMMENT`.
- `functions[].description` → function `COMMENT`.

Updated via `ALTER TABLE ... SET TBLPROPERTIES (...)` and `COMMENT ON COLUMN ...` SQL or REST API. Description provenance is encoded as a `properties` key `ontology_foundry.description_provenance`.

### 5.3 Bindings → tags

- `binds_to.object_type=<id>` Tag with value the card id.
- `binds_to.card_version_seen=<n>` Tag.
- `binds_to.implements_interfaces=<id1>,<id2>` Tag.
- `binds_to.human_reviewed=true|false` Tag.
- Per column: `binds_to.card_field=<field>` Tag, `binds_to.binding_kind=<kind>` Tag.

The Unity Catalog tag namespace is flat; we prefix all our tags with `ontology_foundry.` to avoid pollution.

### 5.4 Markings → Tags

Same prefix pattern:
- `compliance.contains_pii=true`
- `compliance.regulated_hipaa=true`
- `compliance.sensitivity=confidential`

Unity supports value lists per tag key, but our model is single-value per marking. Multi-value markings become repeated tags with the same key and different values where Unity allows it.

### 5.5 Causal claims — degraded

Unity does not have a custom type system. Per `causal_publishing.mode`:

- **`long_form_description`** (default for Unity): claims rendered as text under a section heading in the table's COMMENT, e.g.,
  ```
  ## Causal Knowledge
  
  - Subject: Employee.training_completion_rate
    Predicate: leading_indicator_of
    Object:    compliance_gap
    Confidence: 0.78
    Evidence:  doc:hr-policy-2024#chunk-12; sql_pair:csod_risk_attrition#q42
  ```
- **`skip`**: omit causal knowledge entirely. Publisher returns `partial` with `omits=['causal']`.

### 5.6 Lineage

Push limited. Unity computes lineage from observed query history; manual push is via the Catalog Lineage API (currently limited public surface). The Unity publisher writes lineage where the API supports it and otherwise records the lineage in the table's COMMENT under a `## Lineage` section.

### 5.7 Publish sequence

```
1. Authenticate (PAT or OAuth) against Databricks workspace
2. For each asset in scope (tables/views/materialized views/functions):
   a. Verify catalog + schema exist in Unity; create if configured to do so, else skip
   b. Manifest sha check; skip if unchanged
   c. Upsert the table/view (CREATE OR REPLACE for views, ALTER for existing tables)
   d. Apply column comments
   e. Apply tags (bindings, markings)
   f. Rewrite table COMMENT body with description + causal knowledge + lineage sections
   g. Update publisher_state
3. Surface metrics + errors
```

### 5.8 Deletion / deprecation

`unpublish` does **not** drop Unity tables/views — that's risky (potential to delete data references). It removes the foundry-managed tags and replaces the table COMMENT with an `Asset deprecated in foundry on YYYY-MM-DD` notice. Hard removal is operator-only via Databricks UI/CLI.

---

## 6. DataHub mapper (stub)

DataHub uses a custom aspect/entity model in PDL. The DataHub publisher would:
- Map MDL assets to DataHub's `dataset` entity with platform aspects.
- Map bindings to a custom `canonicalEntityBinding` aspect.
- Map causal claims to a custom `causalClaim` entity with custom aspects.
- Map equivalence classes to a custom `equivalenceClass` entity.

Deferred. Stub interface ships in `ontology_foundry.publishers.datahub` returning `SupportDecision(supported=False, reason="not_implemented")`.

---

## 7. Fidelity scoring

A standardized per-target fidelity score for ops dashboards:

```python
@dataclass
class FidelityScore:
    target_name: str
    asset_rk: str
    overall: float                                # 0..1
    per_concern: dict[str, float]                 # mdl, context, bindings, governance, causal, metrics
    omitted_concerns: list[str]
```

Computed by comparing each concern's bundle representation against what the target actually stores after publish.

Heuristic:
- `mdl` = (columns_present_in_target / columns_present_in_bundle) × (description_present_ratio).
- `context` = whether the bundle's source/catalog/schema context can be reconstructed by walking target relationships.
- `bindings` = (bound_fields_in_target / bound_fields_in_bundle).
- `governance` = ((owners_in_target == owners_in_bundle) AND (sensitivity_in_target == effective_sensitivity_in_bundle)) as 1/0, plus markings_present ratio.
- `causal` = published_claim_count / bundle_claim_count.
- `metrics` = metrics_published / metrics_in_bundle (or 1.0 if no metrics).

Fidelity is reported per publisher per asset; aggregated for dashboards.

---

## 8. Error handling and retries

### 8.1 Per-asset failure isolation

Failure to publish one asset does not block subsequent assets in the same job. Failures accumulate in the job result.

### 8.2 Retry policy

Default exponential backoff: 1s, 5s, 30s, 5min, 30min. After 5 failures, the asset is marked `failed` in `publisher_state` and surfaced to ops.

### 8.3 Permission errors

Caught and reported as `auth_failure` separately from API/data errors. Auth failures fail the entire job (re-running per-asset is pointless until auth is fixed).

### 8.4 Partial-bundle errors

When some concerns succeed and others fail (e.g., classification API rejected a marking but entity update succeeded), the publisher records `partial_with_errors` in `PublishResult` and re-attempts the failed concerns on the next run.

---

## 9. Drift detection (target ↔ foundry)

A scheduled `drift_detector` job runs nightly per configured publisher:

1. For each asset in `publisher_state`, fetch the current target representation.
2. Compute a target-side "round-trip" manifest by mapping target fields back to bundle concerns.
3. Compare against the foundry's current bundle manifest sha.
4. Differences flagged in a `publisher_drift` table:

```sql
CREATE TABLE publisher_drift (
  publisher_name   text NOT NULL,
  asset_rk         text NOT NULL,
  detected_at      timestamptz NOT NULL DEFAULT now(),
  drift_kind       text NOT NULL,   -- 'description_overwritten_in_target' | 'tag_removed_in_target' | 'unknown_field_added_in_target'
  details          jsonb NOT NULL,
  resolved_at      timestamptz,
  PRIMARY KEY (publisher_name, asset_rk, drift_kind, detected_at)
);
```

Drift is **surfaced, not auto-resolved**. The next publish would naturally overwrite target-side changes (we own the published fields); operators decide whether to suppress that with `freeze_concerns` config or accept the foundry's version as canonical.

---

## 10. Test surface

Each publisher must ship with:

1. **Round-trip tests** — publish a known bundle to a mock target, fetch back, assert mapping correctness per concern.
2. **Idempotency tests** — publish twice; assert second call produces zero mutations and `changed_concerns=[]`.
3. **Fidelity floor tests** — for each asset_kind, compute `FidelityScore` and assert `overall >= configured floor` for the target.
4. **Auth failure tests** — bad credentials → `auth_failure`.
5. **Partial-support tests** — Unity publisher with API endpoint → `SupportDecision('partial', omits=['api_endpoint'])`.

Mock targets: Purview's Atlas API has multiple Python stubs available (`atlasclient`-based); Unity Catalog is mockable via `pyspark.sql` test harnesses and the Databricks SDK's mock mode.

---

## 11. Examples

### 11.1 Publish a Snowflake table to Purview

```python
publisher = PurviewPublisher.from_config("acme-purview-prod")
bundle = bundle_store.get("snowflake://acme-prod.analytics.clinical_marts/encounters")

decision = publisher.supports(bundle)
assert decision.supported is True

result = publisher.publish(bundle, actor="ops@acme.com")
print(result)
# PublishResult(asset_rk='snowflake://...', target_id='atlas-guid-...',
#               fidelity='full', omitted_concerns=[],
#               changed_concerns=['governance', 'causal'],
#               target_object_kinds_touched=['snowflake_table', 'glossary_term',
#                                            'ontology_foundry_causal_claim',
#                                            'classification'])
```

### 11.2 Publish a Salesforce endpoint to Unity (degraded)

```python
publisher = UnityCatalogPublisher.from_config("acme-unity-prod")
bundle = bundle_store.get("api://acme-salesforce/standard_objects/Account")

decision = publisher.supports(bundle)
# SupportDecision(supported='partial', omits=['api_endpoint'],
#   reason="Unity Catalog has no native API endpoint asset kind")

# Operator-controlled: publish anyway as a placeholder Table-like description,
# or skip entirely.
result = publisher.publish(bundle, actor="ops@acme.com", dry_run=True)
```

---

## 12. Operations

### 12.1 First-time onboarding (Purview)

1. Configure auth credentials in secrets backend.
2. Add publisher entry to `tenants/<org_id>/publishers.yaml` with `enabled: false`.
3. Run `publisher health` — expect `degraded` because typedefs aren't registered.
4. Run `publisher register-types` (one-shot operator command). Verifies typedefs land.
5. Re-run `publisher health` — expect `healthy`.
6. Run `publisher publish --dry-run --asset-rk <one rk>` to verify mapping on a single asset.
7. Enable the publisher; let cron run.

### 12.2 Rolling back a bad publish

1. Take an inventory: `publisher list-published --since <timestamp>`.
2. For each affected asset, either:
   - Restore prior state by republishing a prior bundle generation (if retained), or
   - `unpublish` to remove foundry-managed attributes; let Purview revert to its pre-foundry state.

### 12.3 Suppressing publish for an asset

Per-asset `freeze_at_target` flag in `publisher_state`. When set, the publisher skips this asset on all subsequent publishes until cleared.

---

## 13. Open items

- **Real-time push mode** for Purview via Event Hubs — Purview's event-driven ingestion supports real-time entity updates; supported in roadmap, not initial implementation. Default stays batch/scheduled.
- **Unity Catalog AI/BI Semantic Model integration** for metrics — currently experimental; revisit when GA.
- **Atlas Glossary hierarchical structure** — currently flat per tenant; consider hierarchical glossaries mirroring the card-kind taxonomy (object_types under one branch, causal_nodes under another).
- **DataHub mapper implementation** — deferred; stub ships.
- **Multi-tenant Atlas glossary collision** — when many tenants publish to one shared Purview instance, glossaries must be namespaced. Spec says per-tenant glossary; verify with the first multi-tenant deployment.

---

## 14. Change log

| Date | Change |
|---|---|
| 2026-05-15 | Initial draft. |
