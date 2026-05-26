# ontology-pipeline

Auto-build pipeline that introspects a configured data source, generates MDL v2,
and enriches each asset with bottoms-up annotations (`concepts`, `key_areas`,
`causal_relations`) against the tenant's semantic-layer cards.

Depends on `ontology-foundry` for the LLM provider abstraction and shared
models. v1 supports **Postgres** as the source kind and **filesystem** as the
output sink. Snowflake / Salesforce / ServiceNow extractors and a `HierarchyStore`
sink land in subsequent versions; this package's `Introspector` / `Sink`
Protocols are the extension seams.

## What it does

```
[Postgres source]
      │
      ▼  PostgresIntrospector
[IntrospectionResult: tables, columns, comments, PKs, FKs, view defs]
      │
      ▼  filter (include/exclude + glob patterns; empty = process all)
[filtered tables]
      │
      ▼  build_mdl()          — deterministic; preserves native COMMENTs verbatim
[MDL skeleton]
      │
      ▼  fill_descriptions()  — LLM fills ONLY missing table/column descriptions
[MDL with descriptions]
      │
      ▼  enrich_annotations() — LLM proposes concepts / key_areas / causal_relations
[MDL + AssetAnnotations]
      │
      ▼  FilesystemSink
<base_dir>/mdl/<source>/<schema>/<table>.json
<base_dir>/annotations/<source>/<schema>/<table>.annotations.json
<base_dir>/run_state.json     — content-hash idempotency
```

Re-runnable. The pipeline computes a content hash per table (DDL shape + native
descriptions + PK/FK). On re-run, tables whose hash matches the prior run are
skipped unless `pipeline.re_enrich_unchanged: true` is set in config.

## Install

```bash
# From the monorepo:
cd packages/ontology-pipeline
pip install -e ".[llm]" -e "../ontology-foundry[llm]"
```

The `[llm]` extra pulls in `openai`. Without it, the pipeline runs in
deterministic-only mode (no descriptions filled, no annotations).

## Run

```bash
# Set required env vars:
export PG_USER=postgres
export PG_PASSWORD=...
export OPENAI_API_KEY=sk-...

# Dry-run: introspect + show what would be processed, no writes, no LLM.
ontology-pipeline dry-run --config configs/example.yaml

# Real run.
ontology-pipeline run --config configs/example.yaml \
                     --output-json out/run-report.json
```

Exit codes:
- `0` — success (zero errored tables).
- `2` — one or more tables errored. Summary still printed; the run-state still
  flushes for tables that succeeded.

## Config shape

See `configs/example.yaml`. Five sections:

| Section | Purpose |
|---|---|
| `source` | source_id / org_id / kind + Postgres connection + schemas to scan |
| `tables` | optional include/exclude lists + glob patterns. Empty = process all |
| `semantic_layer` | path to `<tenant>/semantic_layer/` card tree + key_areas_vocab.yaml |
| `output` | `kind: filesystem` + `base_dir` |
| `llm` | `provider: openai`, `model`, `api_key_env` |
| `pipeline` | switches: `fill_descriptions`, `annotate`, `re_enrich_unchanged` |

Environment variables referenced as `${VAR}` are expanded at load time.

## What gets written

For each processed table:

```
out/acme-corp/
  mdl/csod-servicenow-local/public/csod_employee.json
  annotations/csod-servicenow-local/public/csod_employee.annotations.json
  run_state.json
```

`csod_employee.json` follows the MDL v2 envelope from `mdl_bundle_spec.md` §3
(one entry in `models[]`; `endpoints/functions/metrics/streams` are empty for
tabular sources).

`csod_employee.annotations.json` carries `AssetAnnotations`:

```json
{
  "asset_rk": "postgres://csod-servicenow-local.serviceslearn3_prod_db_02345/public/csod_employee",
  "concepts": ["employee"],
  "key_areas": ["Workforce", "Training_Compliance"],
  "causal_relations": ["overdue_risk", "compliance_gap"],
  "confidence": 0.82,
  "rationale": "Table represents Employee...",
  "source": "llm_enrichment",
  "source_model": "gpt-4o-mini",
  "written_at": "2026-05-17T..."
}
```

`run_state.json` carries per-table content hashes for idempotency:

```json
{
  "format_version": 1,
  "sources": {
    "csod-servicenow-local": {
      "public": {
        "csod_employee": {
          "content_hash": "ab12...",
          "last_outcome": "created",
          "last_seen_at": "2026-05-17T..."
        }
      }
    }
  },
  "updated_at": "2026-05-17T..."
}
```

## Idempotency

- **First run:** all matched tables → `created`.
- **Re-run, no source changes:** all `unchanged`. Zero LLM calls, zero writes.
- **Re-run, source changed for a table:** that table → `updated`. Description fill and annotation re-run only for changed tables.
- **Re-run with `re_enrich_unchanged: true`:** all matched tables reprocess.

## Annotation vocabulary requirements

For the annotation step to do useful work, the configured `semantic_layer.cards_dir`
should contain at minimum some `object_types/<id>.card.md` files. Per
`semantic_layer_card_spec.md`, each card is YAML frontmatter + Markdown body:

```markdown
---
id: employee
layer: semantic
kind: object_type
version: 1
---
An Employee is a person who works at the organization. ...
```

`key_areas_vocab_path` points to a YAML file shaped:

```yaml
version: 1
key_areas:
  - id: Workforce
    description: Employee composition, capacity, and lifecycle.
  - id: Training_Compliance
    description: Mandatory and elective training completion ...
```

If the cards directory is absent or empty, the pipeline runs in
deterministic-only mode for annotation (no proposals; the `concepts[]` etc.
remain empty on the MDL).

## Enrichment stages

Four pluggable LLM-driven stages run after the deterministic MDL build,
parallel to the basic description gap-fill. Each is opt-in via a config flag
and produces structured output that either updates the MDL in place or is
routed to a downstream sink as `side_output`.

| Stage | Class | Adds |
|---|---|---|
| `rich_description` | `RichDescriptionEnricher` | Table-level: `business_purpose`, `primary_use_cases`, `key_relationships`, `update_frequency`, `data_retention`, `access_patterns`, `performance_considerations`. Strict superset of basic `fill_descriptions`. |
| `column_semantics` | `ColumnSemanticsEnricher` | Per-column: `semantic_unit` (e.g. `currency_usd`, `identifier`, `email`, `enum_status`), `business_meaning`, `is_business_key` |
| `data_protection` | `DataProtectionEnricher` | Per-column: `is_pii`, `pii_categories` (names/contact/health/government_id/...), `sensitivity_class` (public/internal/confidential/restricted). Asset-level: suggested RLS predicates + CLS column list (in side_output) |
| `relationship_inference` | `RelationshipInferenceEnricher` | LLM-proposed FKs for tables that lack any declared FK. High-confidence (≥ 0.8) inferences land on `column.properties.references` with `references_provenance: llm_inferred_relationship`; all proposals go to side_output for `lineage_edge` candidates |
| `causal_dependency` | `CausalDependencyEnricher` | LLM-driven causal participation + candidate generation. Uses tenant `causal_node` cards as controlled vocabulary. Per-asset: role declarations (subject / outcome / mediator / moderator) with column-level signals + causal candidate edges (subject→predicate→object) with `confidence` and `mechanism_hint`. Optionally proposes new `causal_node` drafts (review-only — never auto-applied). |

Enable per `pipeline.*` flag in your config:

```yaml
pipeline:
  fill_descriptions: false          # implied off when rich_description is on
  rich_description: true
  enrich_column_semantics: true
  enrich_data_protection: true
  infer_relationships: true
  enrich_causal_dependencies: true
  propose_new_causal_nodes: false   # leave off in prod — review queue can drown ops
  annotate: true
```

All stages preserve native COMMENTs and human-authored values (no-clobber).
Failures surface as `EnrichmentResult.warnings` — never raise.

### Order

```
introspect → build_mdl  (deterministic)
   ↓
[ rich_description | fill_descriptions ]   ← exactly one
   ↓
column_semantics                            ← semantic_unit + business_meaning
   ↓
data_protection                             ← is_pii + sensitivity_class
   ↓
relationship_inference                      ← inferred FKs
   ↓
causal_dependency                           ← causal_node participations + causal candidates
   ↓
annotate                                    ← concepts / key_areas / causal_relations
   ↓
sink.write_mdl + sink.write_annotations
```

### Causal enrichment — scope and honesty

The causal stage runs **at indexing time** against schema + metadata only. It
is necessarily **LLM-driven** because we don't have row data yet. Specifically
it does:

- For each tenant `causal_node` card, ask whether the asset participates and
  in what role, with column-level evidence.
- Propose causal candidate edges with a **controlled predicate vocabulary**
  (causes, caused_by, leading_indicator_of, lagging_indicator_of, moderates,
  mediates, precedes, enables, inhibits, correlates_with) and a confidence.
- Optionally draft new `causal_node` cards when existing vocab doesn't fit —
  drafts land in side_output for human review, never auto-applied.

What it explicitly does NOT do (and why):

- **Statistical causal discovery.** PC, LiNGAM, PCMCI, Granger live in
  `ontology_foundry.causal/` and operate on actual data (numeric profiles,
  time series). Those run as a separate downstream pass against sample data
  after ingest — not at index time. The two paths are complementary: the
  LLM stage proposes candidates with schema-level evidence; the statistical
  stage validates/refutes them against data.
- **Cross-asset inference.** The v1 stage is per-asset. Cross-table causal
  hypotheses (linking two assets' metrics) need multi-asset context — a
  separate stage that runs after the per-asset pass. Roadmap.
- **Auto-apply new causal_node cards.** Vocab discipline matters; new
  causal_node cards always require human review.

Outputs:
- `MDL.model.causal_relations[]` mirrors causal_node ids the asset participates in.
- `MDL.model.causal_participation` (top-level extras block) carries the richer per-role detail.
- `side_output["causal_candidates"]` carries proposed edges for downstream `causal_candidate` table persistence.
- `side_output["proposed_causal_node_drafts"]` carries new-card drafts (review-only).

### Side-output routing

`DataProtectionEnricher.side_output["data_protection_hints"]` carries asset-level
RLS predicate suggestions + CLS column lists. `RelationshipInferenceEnricher.side_output["inferred_relationships"]`
carries inferred FK proposals. `CausalDependencyEnricher.side_output["causal_candidates"]`
carries proposed causal edges; `["proposed_causal_node_drafts"]` carries new card drafts.
v1 logs these; the persistence sink will route them
into appropriate tables (`data_protection_policy`, `lineage_edge` with
`evidence_kind='inferred_relationship'`) as the storage layer expands.

## Extension points

The package is structured so future versions can plug in without rewriting:

| Seam | Protocol | v1 implementation |
|---|---|---|
| Source introspection | `ontology_pipeline.introspect.Introspector` | `PostgresIntrospector` |
| Output sink | `ontology_pipeline.output.Sink` | `FilesystemSink` |
| LLM provider | `ontology_foundry.llm.ModelProvider` | `OpenAIChatProvider` |

The next implementations on the roadmap: `SnowflakeIntrospector`,
`SalesforceIntrospector`, `ServiceNowIntrospector`,
`HierarchyStoreSink` (writes to Postgres + Qdrant per the persistence spec).

## Tests

```bash
cd packages/ontology-pipeline
pytest tests/
```

The smoke test runs in deterministic-only mode against a fixture introspection
result; no live Postgres or LLM required.

## Limitations (v1)

- Postgres only. Snowflake / Salesforce / ServiceNow extractors are roadmap.
- Filesystem sink only. `HierarchyStoreSink` lands when amundsenrds + sidecar
  storage is wired.
- Sequential per-table processing. The config carries a `parallelism` field
  reserved for the parallel/Temporal version.
- View `depends_on` parsing is not yet implemented; views emit with empty
  `view_definition.depends_on[]` even though the view body is captured.
- LLM call coalescing (one call combining description fill + annotation) is
  not done; v1 makes two separate calls per processed table.

## See also

In `nexcraft/nexcraftontologyoss/`:
- `mdl_auto_generation_from_source_spec.md` — the spec this implements.
- `mdl_table_concept_annotation_spec.md` — annotation step's posture.
- `semantic_layer_card_spec.md` — card format used by `annotate`.
- `mdl_bundle_spec.md` — MDL v2 envelope produced.
- `storage_topology_reference.md` — where this pipeline's outputs land in the
  bigger storage picture.
