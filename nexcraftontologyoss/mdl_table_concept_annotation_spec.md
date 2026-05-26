# MDL Table Concept Annotation — Specification

**Status:** Draft 2026-05-16.
**Part of:** Data Knowledge Hierarchy series.
**Depends on:** `mdl_bundle_spec.md`, `T2_to_T6_amundsenrds_sidecar_spec.md`, `semantic_layer_card_spec.md`, `hierarchy_persistence_and_ingestion_spec.md`, `evaluation_harness_spec.md`.
**Pivot:** Replaces the speculative project_registry / project_asset design with a bottoms-up table-level annotation layer.
**Pipeline posture:** **Greenfield.** Does not migrate existing `sql_meta/<project>/project_metadata.json`. New pipeline runs in parallel; cutover by validation, not by migration.

---

## 1. Scope

This spec defines a table-level annotation layer that grounds the ontology graph from the bottom up. Every asset (table, API endpoint, function, metric) carries three new fields directly on its MDL block:

- **`concepts`** — `object_type` card ids this asset embodies.
- **`key_areas`** — strategic business themes the asset serves.
- **`causal_relations`** — `causal_node` card ids this asset participates in.

These replace the `project_id` / `project_registry` scoping mechanism. Projects, if they exist for any UI purpose, derive from `(concepts, key_areas)` clustering — they are no longer authored.

This spec covers the field shapes, vocabularies, storage, the auto-apply LLM enrichment pipeline, validation gates, consumer impact, and the parallel-pipeline test isolation strategy.

Out of scope:
- The semantic layer card format itself (`semantic_layer_card_spec.md`).
- The bundle wire format (`mdl_bundle_spec.md`).
- The CSOD workflow consumer changes (deferred follow-up).

---

## 2. The annotation layer

### 2.1 Per-asset MDL block extension

```json
{
  "name": "csod_employee",
  "rk": "snowflake://acme-prod.csod/public/csod_employee",
  "description": "Cornerstone OnDemand employee master, sourced nightly from the HRIS pipeline. Carries identity, role, department, and employment_status; the primary anchor for all training and compliance analytics.",
  "concepts": ["employee"],
  "key_areas": ["Workforce", "Training_Compliance", "HIPAA"],
  "causal_relations": ["overdue_risk", "compliance_gap", "phishing_risk"],
  "tableReference": { "table": "csod_employee" },
  "materialization": { "kind": "table", "is_materialized": false },
  "view_definition": null,
  "columns": [ /* unchanged */ ]
}
```

Same three fields on:
- `models[]` — tables, views, materialized views.
- `endpoints[]` — API endpoints.
- `functions[]` — UDFs.
- `metrics[]` — semantic-layer metrics.

Empty arrays are valid; assets that haven't been enriched yet just present empty lists. Backward-compatible with any v2 MDL that didn't carry these fields.

### 2.2 Cardinality semantics

| Field | Cardinality | Order |
|---|---|---|
| `concepts` | 0..N; typical 1–3 | Most-primary first. `concepts[0]` carries the "primary object_type" semantics formerly in `semantic_bindings.json.primary_object_type`. |
| `key_areas` | 0..N; typical 1–4 | No order semantics. |
| `causal_relations` | 0..N; typical 0–5 | No order semantics. |

### 2.3 Junction tables, multi-concept tables

Some tables represent multiple `object_type`s simultaneously. The classic case is a junction table — `training_assignment` represents both the assignment-as-entity AND the relationship between Employee and Course. The array shape handles this naturally:

```json
{
  "name": "training_assignment",
  "concepts": ["training_assignment"],          // the primary; the entity-in-its-own-right
  "key_areas": ["Training_Compliance", "Workforce"],
  "causal_relations": ["overdue_risk", "compliance_gap"]
}
```

`training_assignment` is treated as its own object_type (the example card you provided already models it that way). The fact that it relates `employee` to `course` is encoded in the card body and via the card's `refs[]`, not by listing all three concepts on the table.

For genuinely multi-concept tables that don't have a unifying card (rare, usually a sign of denormalization), list all concepts the table embodies; the consumer treats them as equally primary.

---

## 3. Vocabularies

### 3.1 `concepts` — resolves against the card index

Each entry must resolve to an existing card with `kind: object_type` in the tenant card index (tenant ∪ pack overlay; see `semantic_layer_card_spec.md` §4.2).

### 3.2 `causal_relations` — resolves against the card index

Each entry must resolve to an existing card with `kind: causal_node`.

### 3.3 `key_areas` — controlled per-tenant vocabulary

Tenant-level vocab in `tenants/<org_id>/key_areas_vocab.yaml`:

```yaml
version: 1
key_areas:
  - id: Workforce
    description: Employee composition, capacity, and lifecycle.
  - id: Training_Compliance
    description: Mandatory and elective training completion against regulatory or org policy.
  - id: HIPAA
    description: Protection of patient health information per HIPAA.
  - id: SOX
    description: Sarbanes-Oxley financial controls.
  - id: Clinical_Operations
    description: Day-to-day clinical care delivery metrics.
  - id: Patient_Safety
    description: Incidents and near-misses impacting patient outcomes.
  - id: Revenue_Cycle
    description: Billing, collections, denial management.
  - id: Workforce_Risk
    description: Attrition, burnout, escalation patterns.
  - id: Phishing_Risk
    description: Social-engineering and email-borne threats.
  # ... extensible per tenant
```

Each entry has an `id` (used in `key_areas[]` arrays) and a `description` (used by the LLM as context for assignment + by the planner as scoping signal).

### 3.4 Distinguishing `key_areas` from `domain_tags`

| | `domain_tags` (already specced) | `key_areas` (this spec) |
|---|---|---|
| Where attached | Schema (`schema_ext.domain_tags`) | Asset (`table_ext.key_areas`, etc.) |
| What it expresses | Organizational ownership / functional area | Strategic / business themes / risk areas |
| Examples | HR, Finance, Security, IT_Operations, Clinical | HIPAA, Training_Compliance, Patient_Safety, Workforce_Risk |
| Granularity | Coarse — schema-level | Finer — asset-level |
| Vocabulary | Platform seed + per-tenant extension | Per-tenant authored |

A schema might be `domain_tags: [HR, Compliance]` (who owns it); the assets within it carry `key_areas` like `Training_Compliance` or `HIPAA` (what they serve). Both are faceted multi-valued; both flow into the planner's scoping decisions, with `key_areas` carrying more semantic weight because it reflects business intent.

---

## 4. Storage

### 4.1 Columns on extension tables

Each asset_kind's extension table gains the three columns:

```sql
ALTER TABLE table_ext
  ADD COLUMN concepts          text[] NOT NULL DEFAULT '{}',
  ADD COLUMN key_areas         text[] NOT NULL DEFAULT '{}',
  ADD COLUMN causal_relations  text[] NOT NULL DEFAULT '{}';

CREATE INDEX idx_table_ext_concepts          ON table_ext USING gin (concepts);
CREATE INDEX idx_table_ext_key_areas         ON table_ext USING gin (key_areas);
CREATE INDEX idx_table_ext_causal_relations  ON table_ext USING gin (causal_relations);
```

Same three columns + GIN indexes on `api_endpoint_ext`, `function_ext` (when introduced), `metric_ext` (when introduced).

GIN indexes make `concepts && $anchor_concepts` queries cheap regardless of tenant size — the dominant query pattern from the consumer / planner side.

### 4.2 Annotation provenance audit

Each annotation write goes through `hierarchy_audit` with `tier='asset_annotation'` and the field path (`concepts` / `key_areas` / `causal_relations`). The audit log is the provenance store; the columns hold current values.

For per-annotation provenance lookup, a small sidecar:

```sql
CREATE TABLE asset_annotation_provenance (
  asset_rk        text NOT NULL,
  field           text NOT NULL,           -- 'concepts' | 'key_areas' | 'causal_relations'
  source          text NOT NULL,           -- 'llm_enrichment' | 'rule_<service_name>' | 'human'
  source_model    text,                    -- when source='llm_enrichment'
  confidence      real,                    -- LLM self-confidence, when applicable
  written_by      text NOT NULL,           -- actor identity
  written_at      timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (asset_rk, field, written_at)
);
```

Append-only. Provenance lookups for "who set this asset's concepts last?" become `ORDER BY written_at DESC LIMIT 1`. Useful when downstream services (per §5.4) need to know whether a value is LLM-proposed vs. human-confirmed before overwriting.

---

## 5. Auto-apply enrichment pipeline

The pipeline that proposes and **auto-applies** annotations on MDL ingest. No human-in-loop for the initial population; quality is improved by downstream services rather than gating ingest.

### 5.1 Trigger

Runs on:
- First-time ingestion of a new asset (via the databuilder pipeline; see `hierarchy_persistence_and_ingestion_spec.md` §10).
- Re-ingestion where the asset's description, columns, or schema context has changed materially (detected via content hash).
- Explicit operator request via `enrich_asset(asset_rk)`.

Does **not** auto-rerun on every ingest if the asset's source content hasn't changed — that would churn audit + cost LLM tokens for no signal.

### 5.2 Pipeline shape

```python
class AssetAnnotationEnricher:
    def enrich(self, asset_rk: str, *, actor: str = "system") -> EnrichmentResult:
        # 1. Pull current state from storage
        asset      = self.store.get_asset(asset_rk)        # mdl + columns + descriptions
        schema_ctx = self.store.get_schema_view(asset.schema_rk)  # domain_tags, purpose
        source_ctx = self.store.get_source(asset.source_id)       # purpose, business_context

        # 2. Pull candidate cards
        candidate_object_types = self.store.cards.list_by_kind(
            tenant_id=actor_tenant, kind="object_type", include_deprecated=False
        )
        candidate_causal_nodes = self.store.cards.list_by_kind(
            tenant_id=actor_tenant, kind="causal_node", include_deprecated=False
        )
        key_areas_vocab = self.store.get_key_areas_vocab(actor_tenant)

        # 3. LLM-propose
        proposal = self.llm.propose_annotations(
            asset=asset, schema_ctx=schema_ctx, source_ctx=source_ctx,
            candidate_object_types=candidate_object_types,
            candidate_causal_nodes=candidate_causal_nodes,
            key_areas_vocab=key_areas_vocab,
        )

        # 4. Filter to valid (every concept/causal must resolve; key_areas must be in vocab)
        filtered = filter_to_valid(proposal,
                                   card_universe=self.store.cards.resolver_set(actor_tenant),
                                   key_areas_universe=key_areas_vocab)

        # 5. Auto-apply (subject to §5.3 no-clobber rule)
        self.store.set_asset_annotations(asset_rk,
                                         concepts=filtered.concepts,
                                         key_areas=filtered.key_areas,
                                         causal_relations=filtered.causal_relations,
                                         provenance=("llm_enrichment", self.llm.model_id,
                                                     filtered.confidence),
                                         actor=actor)

        # 6. Return result for telemetry
        return EnrichmentResult(asset_rk=asset_rk, filtered=filtered,
                                drift_against_previous=...)
```

### 5.3 No-clobber rule for human / service overrides

The enricher must not overwrite annotations whose latest provenance is `human` or `rule_<service_name>`. Lookup via `asset_annotation_provenance`:

```python
def latest_provenance(asset_rk, field) -> str | None: ...

# Before writing:
for field in ("concepts", "key_areas", "causal_relations"):
    if latest_provenance(asset_rk, field) in ("human", "rule_*"):
        skip(field)  # leave existing value intact; record decision in audit
```

This is the lever that makes "auto-apply with service improvement" safe: services can write better annotations and the LLM won't undo them on the next ingest pass.

### 5.4 Where downstream services fit

Per the user's pipeline-first stance: data-quality / annotation-improvement services are *separate processes* that read assets, apply richer reasoning (cross-asset patterns, lineage analysis, behavioral profiling), and write improved annotations with `source='rule_<service_name>'`. Examples:

| Service | What it improves |
|---|---|
| `concept_disambiguation` | When the LLM tagged an asset with `[employee, worker]`, picks one based on column-name signals. |
| `causal_relation_validator` | Verifies declared causal_relations are consistent with the causal_node's subject_refs/outcome_refs; removes mis-tagged. |
| `key_areas_propagator` | If a schema has `domain_tags: [Clinical]`, propagates a likely `Clinical_Operations` key_area onto its assets. |
| `manual_correction_console` | Operator-facing UI; writes `source='human'`. |

None of these are in scope for *this* spec; the spec just establishes the contract that they can plug in by writing rows with appropriate provenance.

### 5.5 LLM prompt shape

```
SYSTEM: You annotate data assets with their semantic identity. Output ONLY structured JSON.

ASSET:
  rk: {asset_rk}
  kind: {asset_kind}
  name: {name}
  description: {description}
  schema: {schema_name}    purpose: {schema_purpose}    domain_tags: {domain_tags}
  source: {source_name}    purpose: {source_purpose}
  columns:
    {column_name}: {col_type}  — {column_description}
    ...

CANDIDATE object_type CARDS (pick the most specific that match):
  - id: employee
    body_excerpt: An Employee is a person who works at the organization. Each employee ...
  - id: training_assignment
    body_excerpt: A TrainingAssignment is the link between an employee and a course ...
  ...

CANDIDATE causal_node CARDS (pick those this asset feeds; may be empty):
  - id: overdue_risk
    body_excerpt: OverdueRisk is the per-employee risk that one or more required training ...
  - id: compliance_gap
    body_excerpt: ComplianceGap is the per-department roll-up risk of training non-completion ...
  ...

KEY_AREAS VOCABULARY:
  - id: Workforce, description: Employee composition, capacity, and lifecycle.
  - id: Training_Compliance, description: Mandatory and elective training completion ...
  ...

OUTPUT JSON SCHEMA:
{
  "concepts":           [card_id, ...],         // 0-3, ordered most-primary first
  "key_areas":          [key_area_id, ...],     // 0-4
  "causal_relations":   [causal_node_id, ...],  // 0-5, may be empty
  "confidence":         0.0-1.0,
  "rationale":          "one-paragraph explanation"
}
```

Token budget for this call: typically 3–8K depending on candidate-card count. Pack-shipped cards reduce per-tenant tokens for shared concepts.

The `rationale` field is captured in `asset_annotation_provenance.rationale` (additional column not shown in §4.2; add at implementation time) — useful for downstream services that want to know *why* the LLM picked what it did.

### 5.6 Confidence threshold

No threshold for auto-apply. Every LLM proposal that passes the post-filter (every id resolves) auto-applies. This is the explicit posture for the iteration phase: ship rough, fix with services. Tighten later under the eval harness if needed.

### 5.7 Idempotency

Re-running enrichment on the same asset with the same source content produces the same annotations (modulo LLM nondeterminism, which the system tolerates). The no-clobber rule prevents re-overwriting human/service edits; the content-hash check (§5.1) prevents re-running when no upstream change has occurred.

---

## 6. Validation gates

All gates warn but do not block ingest. Warnings surface to:
- The `hierarchy_audit` table with `action='annotation_warning'`.
- The ops dashboard's annotation-health view.
- **NOT** the end-user-facing UI. End users never see ingest-time annotation warnings.

### 6.1 Gate list

| Gate | Check | Severity |
|---|---|---|
| `concepts_resolve` | Every entry in `concepts[]` resolves to an `object_type` card | warn |
| `causal_relations_resolve` | Every entry resolves to a `causal_node` card | warn |
| `key_areas_in_vocab` | Every entry is in the tenant's `key_areas_vocab.yaml` | warn |
| `concepts_nonempty_for_production_assets` | `lifecycle_stage='production'` assets have non-empty `concepts[]` | warn |
| `causal_consistency` | For each `causal_relations[k]`, `concepts[]` overlaps with that causal_node's `subject_refs` OR `outcome_refs` | warn |

### 6.2 Implementation

Added as named functions in `ontology_foundry/eval/gates.py`, same `(GateVerdict, list[EvalIssue])` return shape as the existing gates. Each gate emits structured `EvalIssue` with:

```python
EvalIssue(
    code="UNRESOLVED_CONCEPT",
    message=f"Asset {asset_rk} declares concept {bad_id!r} which has no matching object_type card",
    severity="warn",
)
```

The eval harness (`evaluation_harness_spec.md`) aggregates these into the annotation-health metric. A high warning count is a signal for service intervention, not a blocking failure.

### 6.3 Auto-fix attempts

When a gate fires `UNRESOLVED_CONCEPT`, the enricher's next pass may attempt to either:
- Drop the unresolved id (most conservative).
- Substitute the nearest resolvable id by semantic similarity (when the misspelling / variant is obvious).

This is a service-side concern (§5.4 `concept_disambiguation`), not part of the validation gate itself.

---

## 7. Consumer impact

### 7.1 `semantic_bindings.json` — drop `primary_object_type`

The `primary_object_type` field is removed from the bindings file. Consumers derive it from `mdl.concepts[0]` at read time:

```python
@property
def primary_object_type(self) -> str | None:
    return (self.mdl.get("models", [{}])[0].get("concepts") or [None])[0]
```

`semantic_bindings.json` continues to carry per-field bindings (`binding_field[]`); the asset-level concept linkage is now redundant with MDL.

### 7.2 Planner uses concept/key_area/causal scoping

The planner's structured output drops `project_ids` and substitutes:

```json
{
  "anchors":           ["employee", "training_assignment"],
  "intent":            "COMPLIANCE_REC",
  "plan_template_id":  "compliance_dashboard_rec",
  "scope": {
    "concepts":         ["employee", "training_assignment"],
    "key_areas":        ["HIPAA", "Training_Compliance"],
    "causal_relations": ["overdue_risk", "compliance_gap"]
  },
  "params":            { "time_window": "last_quarter", "segments": ["clinical"] },
  "confidence":        0.86
}
```

Scoped retrieval:

```sql
SELECT t.rk, t.name, ae.effective_sensitivity_class, ae.effective_freshness_sla
FROM v_asset t
JOIN table_ext te ON te.table_rk = t.rk
JOIN v_asset_effective ae ON ae.asset_rk = t.rk
WHERE te.concepts          && $scope.concepts
   OR te.key_areas         && $scope.key_areas
   OR te.causal_relations  && $scope.causal_relations
ORDER BY (cardinality(te.concepts & $scope.concepts) +
          cardinality(te.key_areas & $scope.key_areas) +
          cardinality(te.causal_relations & $scope.causal_relations)) DESC
LIMIT 30;
```

### 7.3 `BundleStore.list_assets` filter additions

```python
def list_assets(self, *,
                ...,
                concepts: list[str] | None = None,
                key_areas: list[str] | None = None,
                causal_relations: list[str] | None = None,
                ...) -> AssetPage: ...
```

Each operates as `table_ext.<field> && $value` (any-overlap). Multi-filter is AND across fields. The convenience filter `canonical_entity` from the consumer spec aliases to `concepts=[entity_id]`.

### 7.4 `OntologyContextLoader` no longer needs project_ids

Anchor resolution returns cards directly (no project step). The loader's `intent` recipes already filter by edge kinds and card kinds; no changes there.

---

## 8. Pipeline isolation — running the new path in parallel

The user's explicit posture: **no migration; build a new pipeline; test it separately.**

### 8.1 Namespace separation

The new pipeline writes to the same `table_ext` / `api_endpoint_ext` / etc. tables — the columns are additive, so existing tables don't break. **But** new pipeline runs gate on a tenant-scope flag:

```yaml
# tenants/<org_id>/pipeline_config.yaml
bottoms_up_annotation_pipeline:
  enabled: true                            # gate; false disables auto-enrichment
  enrichment_model: "claude-opus-4-7"      # LLM provider
  candidate_card_limit: 50                 # top-K cards shown to LLM per enrichment
  re_enrich_on_description_change: true
  audit_to_dedicated_log: bottoms_up_enrichment_audit.log
```

When `enabled: false`, the enricher doesn't run; columns remain at default empty. The asset behaves exactly as it did before the spec, and any consumer reading those columns sees empty arrays (the planner falls back to its prior project-based scoping path).

When `enabled: true`, every ingested asset goes through the enricher.

### 8.2 Test tenant

Initial validation: a dedicated test tenant (`acme-corp-test` or similar) with `enabled: true`. Production tenant stays `enabled: false` until validation passes.

### 8.3 Validation suites

Run the eval harness's evals against the test tenant:

| Eval | Pass criterion |
|---|---|
| Annotation coverage | ≥ 80% of production-lifecycle assets have non-empty `concepts[]` after enrichment runs |
| Concept resolution | ≥ 95% of asserted concept ids resolve to existing cards (the rest produce warnings, not failures) |
| Causal consistency | ≥ 90% of asserted causal_relations pass the consistency gate |
| Planner round-trip | For the curated CSOD question corpus (`evaluation_harness_spec.md` §2), planner-with-concepts produces equivalent-or-better `Context Sufficiency` scores vs planner-with-project-ids |
| Answer quality | Equivalent-or-better `quality_A` score on the corpus's eval set |
| Latency | p95 planner latency does not regress |

When all five pass for ≥ 2 weeks of nightly runs, the production tenant flips `enabled: true`.

### 8.4 Cutover

The cutover is a config flip per tenant. No data migration. The existing `sql_meta/<project>/project_metadata.json` files stay where they are; they're simply no longer consulted by the planner once the bottoms-up path is live. They can be archived at a later cleanup pass.

### 8.5 Rollback

Set `enabled: false`. Columns retain their populated values (which the next consumer query would still see), so if a partial rollback is desired, leave the data in place and let the planner ignore it. For a full rollback, run `clear_annotations(tenant_id)` which empties the three columns across all assets and the provenance log captures the rollback.

---

## 9. Cross-spec amendments

The following changes are needed in existing specs when this pipeline lands. They are NOT applied in this turn; apply when the pipeline is built or via a consolidated update.

| Spec | Section | Change |
|---|---|---|
| `mdl_bundle_spec.md` | §3.2 (model block) | Add `concepts`, `key_areas`, `causal_relations` fields. |
| `mdl_bundle_spec.md` | §3.3 (endpoint), §3.4 (function), §3.5 (metric) | Same three fields. |
| `mdl_bundle_spec.md` | §5 (`semantic_bindings.json`) | Remove `primary_object_type` field from the JSON shape. Add note: derived from `mdl.concepts[0]` at consumer time. |
| `T2_to_T6_amundsenrds_sidecar_spec.md` | §6.1 (`table_ext`) | Add three columns + GIN indexes per §4.1 above. |
| `T2_to_T6_amundsenrds_sidecar_spec.md` | §6.2, §6.3, §6.4 (other ext tables) | Same three columns where the ext tables exist. |
| `hierarchy_persistence_and_ingestion_spec.md` | §10 (databuilder integration) | Add `AssetAnnotationEnricher` as a post-load step that runs on the queue per §5 here. |
| `hierarchy_persistence_and_ingestion_spec.md` | §14 (workers) | Add `enrichment-worker`. |
| `bundle_consumer_api_spec.md` | §2 (`BundleStore`) | Add `concepts`, `key_areas`, `causal_relations` filter params to `list_assets`. |
| `evaluation_harness_spec.md` | §9 (card-specific evals) | Add the five gates from §6.1 here to the corpus health checks (warn level). |
| `semantic_layer_card_spec.md` | §12.1 (card prose names the binding) | Clarify: the *machine-readable* binding now lives on the MDL via `concepts[]`, not in `semantic_bindings.json.primary_object_type`. Card prose continues to describe the relationship for the LLM; the gate that verifies card↔MDL consistency reads from `concepts[]`. |

The `project_registry` / `project_asset` tables proposed in the prior planning turn are **not** introduced — they're superseded by this spec.

---

## 10. Telemetry

Per-enrichment-run telemetry recorded for the eval harness:

```json
{
  "event": "asset_annotation_enriched",
  "asset_rk": "...",
  "asset_kind": "table",
  "concepts_proposed": ["employee"],
  "concepts_applied": ["employee"],
  "concepts_skipped_clobber": [],
  "key_areas_proposed": ["Workforce", "Training_Compliance"],
  "key_areas_applied": ["Workforce", "Training_Compliance"],
  "causal_relations_proposed": ["overdue_risk", "compliance_gap"],
  "causal_relations_applied": ["overdue_risk", "compliance_gap"],
  "confidence": 0.84,
  "llm_model": "claude-opus-4-7",
  "wall_time_ms": 1240,
  "tokens_in": 4218,
  "tokens_out": 312,
  "warnings_emitted": []
}
```

Aggregations: enrichment cost per source per day, warning rates per gate, no-clobber-skip rates.

---

## 11. Operations

### 11.1 First-time enrichment of an existing tenant

```bash
# Bootstrap: enrich every existing production asset that has empty concepts[]
python -m ontology_foundry.enrichment.bootstrap \
  --tenant-id acme-corp-test \
  --asset-kinds table,view,materialized_view,api_endpoint,metric \
  --lifecycle-stages production \
  --only-if-empty \
  --batch-size 50 \
  --concurrency 4
```

Reports total assets enriched, per-asset token cost, gate-warning summary.

### 11.2 Targeted re-enrichment

```bash
# Re-enrich a specific asset after upstream description changed
python -m ontology_foundry.enrichment.enrich \
  --asset-rk "snowflake://acme-prod.csod/public/csod_employee"
```

### 11.3 Service intervention

Services writing improved annotations use `HierarchyStore.set_asset_annotations(..., provenance=("rule_concept_disambiguation", None, 0.97))`. The provenance source identifies the service for audit; subsequent LLM enrichment respects the no-clobber rule.

---

## 12. Open items

- **Annotation richness beyond three fields.** This spec defines exactly three annotation fields. Future additions (e.g., `data_quality_signals[]`, `consumer_patterns[]`) would extend the schema. Defer until first concrete need.
- **Cross-tenant annotation borrowing.** If two tenants both have an `employee` concept, can one borrow the other's well-curated annotations as a starting point? Privacy concerns. Defer.
- **Negative annotations.** Currently no way to say "this asset is *not* about Employee even though it has an `employee_id` column." If misclassification by the LLM becomes a recurring pattern, add `excluded_concepts[]`. Defer until the first observed need.
- **Multi-language annotation.** If a tenant supports multiple languages, do `key_areas[]` ids stay English (recommended) or get localized labels? Defer; treat ids as English-only and localize the descriptions.
- **Annotation versioning.** Currently the latest annotation wins (with provenance audit). If we want to retain annotation history (similar to card versioning), add a `version` column. Defer.

---

## 13. Change log

| Date | Change |
|---|---|
| 2026-05-16 | Initial draft. Bottoms-up pivot; replaces speculative project_registry from prior planning. |
