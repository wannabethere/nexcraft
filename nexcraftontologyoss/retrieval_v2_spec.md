# Retrieval v2 — Specification

**Status:** Draft 2026-05-17.
**Part of:** Data Knowledge Hierarchy series.
**Depends on:** `bundle_consumer_api_spec.md`, `semantic_layer_card_spec.md`, `mdl_table_concept_annotation_spec.md`, `T2_to_T6_amundsenrds_sidecar_spec.md`, `mdl_bundle_spec.md`.
**Replaces (at consumer call sites):** `genieml/agents/app/agents/retrieval/` — the project-id-keyed Chroma-backed retrieval module.
**Compatibility posture:** Sibling module; old module stays alongside until migration is complete. New module mirrors the old API shape where useful and substitutes ontology-graph backings underneath.

---

## 1. Scope

This spec defines a new retrieval module that fronts the ontology-graph storage stack (BundleStore + CardStore + OntologyContextLoader) with an interface shaped like the existing `genieml/agents/app/agents/retrieval/RetrievalHelper` and its focused retrievers (`TableRetrieval`, `SqlPairsRetrieval`, `Instructions`, `HistoricalQuestionRetrieval`, `SqlFunctions`).

The module:
1. Exposes a familiar façade so caller sites in compliance-skill, CSOD workflows, and other genieml services migrate with minimal change.
2. Replaces `project_id` filtering with **concepts / key_areas / causal_relations / source_id / asset_kind** filtering — the bottoms-up scoping primitives.
3. Returns **bundles or bundle excerpts** instead of raw documents where applicable.
4. Adds new retrievers for surfaces the old module didn't cover (cards, lineage, claims, code lists).
5. Defines new Qdrant collections for sql_pairs and historical Q&A (which weren't first-class in the prior spec stack).

Out of scope:
- Re-implementing BundleStore / CardStore / OntologyContextLoader (covered in `bundle_consumer_api_spec`).
- The MCP server (`mcp_qa_agents_spec`); the MCP server's tools call this module internally.

---

## 2. Module structure

```
ontology_foundry/consumer/retrieval/
  __init__.py
  retrieval_helper.py            # façade — RetrievalHelperV2
  asset_retrieval.py             # AssetRetrieval (tables/views/mvs/api_endpoints/functions/metrics)
  card_retrieval.py              # CardRetrieval (object_type, causal_node, derived_state, interface, action, metric, event, instruction)
  sql_pairs_retrieval.py         # SqlPairsRetrievalV2
  instructions_retrieval.py      # InstructionsRetrievalV2 (cards + legacy InstructionService)
  historical_qa_retrieval.py     # HistoricalQARetrievalV2
  sql_functions_retrieval.py     # SqlFunctionsRetrievalV2 (thin wrapper over asset_retrieval with asset_kind=function)
  metric_retrieval.py            # MetricRetrievalV2 (asset_kind=metric + metric.json sidecar)
  lineage_retrieval.py           # LineageRetrievalV2 (lineage_edge traversal)
  claim_retrieval.py             # ClaimRetrievalV2 (causal claims + candidates)
  code_list_retrieval.py         # CodeListRetrievalV2 (T6 lookups)
  schema_pruning.py              # PreprocessSqlData equivalent — schema truncation/pruning
  models.py                      # shared Pydantic models for results
  scope.py                       # RetrievalScope dataclass + resolution helpers
```

Mirrors the structure of `genieml/agents/app/agents/retrieval/` so engineers familiar with that module can navigate the new one. The implementations underneath are different.

---

## 3. The scope model

The old module's `project_id` parameter is replaced by a richer `RetrievalScope`:

```python
@dataclass
class RetrievalScope:
    """Scoping primitives for retrieval. Replaces project_id."""
    org_id:               str                                      # always required
    source_ids:           list[str] | None = None                  # restrict to sources
    catalog_uids:         list[str] | None = None
    schema_rks:           list[str] | None = None
    concepts:             list[str] | None = None                  # card ids — object_type
    key_areas:            list[str] | None = None                  # vocabulary entries
    causal_relations:     list[str] | None = None                  # card ids — causal_node
    lifecycle_stages:     list[str] | None = None
    include_deprecated:   bool = False
    asset_kinds:          list[str] | None = None
    sensitivity_max:      str | None = None                        # e.g. 'confidential'
    compliance_regimes:   list[str] | None = None

    # Backward-compat shim (single field for legacy callers)
    legacy_project_id:    str | None = None

    @classmethod
    def for_project(cls, project_id: str, *, org_id: str) -> "RetrievalScope":
        """Constructor for legacy callers passing a project_id.
        Resolves project_id → (concepts, key_areas) via a registry lookup
        when the bottoms-up pipeline is enabled; otherwise falls through to
        legacy_project_id for the old retrieval path."""
        ...
```

### 3.1 Scope resolution

`RetrievalScope` is resolved at the boundary of every retriever call. If a scope carries `legacy_project_id` only and no other filters, the retriever falls back to direct project-id Chroma access (for the deprecation period). Otherwise the new path is taken.

Resolution prefers the most specific filter:
- `schema_rks` > `catalog_uids` > `source_ids` > `concepts ∪ key_areas`.
- Multiple filters AND together at the SQL level (`v_asset_effective` filters).

### 3.2 The translation from `legacy_project_id`

A small translation table (one-time bootstrap per tenant):

```sql
CREATE TABLE legacy_project_translation (
  org_id            text NOT NULL,
  legacy_project_id text NOT NULL,
  concepts          text[] NOT NULL DEFAULT '{}',
  key_areas         text[] NOT NULL DEFAULT '{}',
  source_ids        text[] NOT NULL DEFAULT '{}',
  PRIMARY KEY (org_id, legacy_project_id)
);
```

Populated by an operator-run migration: for each `sql_meta/<project_id>/project_metadata.json`, derive the dominant concepts (from current `table_ext.concepts` of its referenced tables), key_areas, and source_ids. The translation lets callers using `project_id` get reasonable behavior on the new path without code changes.

When `bottoms_up_annotation_pipeline.enabled = false` (per `mdl_table_concept_annotation_spec.md` §8.1), `legacy_project_id` routes through the old retrieval module untouched.

---

## 4. Façade — `RetrievalHelperV2`

```python
class RetrievalHelperV2:
    def __init__(self,
                 *,
                 bundle_store: BundleStore,
                 card_store: CardStore,
                 context_loader: OntologyContextLoader,
                 legacy_helper: RetrievalHelper | None = None):
        """
        legacy_helper allows transparent fallback for scopes that carry only
        legacy_project_id and have no concept/key_area translation. Set to
        None to disable fallback (post-migration).
        """
        ...

    # ──────────────────────────────────────────────────────────────────
    # Asset-side retrievers (mirror old TableRetrieval surface)
    # ──────────────────────────────────────────────────────────────────
    async def get_database_schemas(self, *, scope: RetrievalScope) -> list[SchemaContext]: ...
    async def get_table_names_and_schema_contexts(self,
                                                  query: str,
                                                  *, scope: RetrievalScope,
                                                  k: int = 5) -> list[TableContext]: ...
    async def get_views(self, *, scope: RetrievalScope) -> list[ViewBundle]: ...
    async def get_metrics(self, *, scope: RetrievalScope) -> list[MetricBundle]: ...
    async def get_table_columns(self, *, table_rk: str) -> list[ColumnInfo]: ...

    # ──────────────────────────────────────────────────────────────────
    # Card-side retrievers (new)
    # ──────────────────────────────────────────────────────────────────
    async def get_concepts(self, query: str, *, scope: RetrievalScope, k: int = 10) -> list[CardHit]: ...
    async def get_causal_nodes(self, query: str, *, scope: RetrievalScope, k: int = 10) -> list[CardHit]: ...
    async def get_derived_states(self, *, attached_to: str, scope: RetrievalScope) -> list[CardHit]: ...
    async def get_actions(self, *, operates_on: str, scope: RetrievalScope) -> list[CardHit]: ...
    async def get_card_body(self, *, card_id: str, kind: str) -> CardView: ...

    # ──────────────────────────────────────────────────────────────────
    # SQL pair / instruction / historical (parallel old surface)
    # ──────────────────────────────────────────────────────────────────
    async def get_sql_pairs(self, query: str, *, scope: RetrievalScope, k: int = 10) -> list[SqlPairHit]: ...
    async def get_instructions(self, query: str, *, scope: RetrievalScope, k: int = 30) -> list[InstructionHit]: ...
    async def get_historical_questions(self, query: str, *, scope: RetrievalScope, k: int = 10) -> list[HistoricalQAHit]: ...
    async def get_sql_functions(self, query: str, *, scope: RetrievalScope, k: int = 10) -> list[FunctionBundle]: ...

    # ──────────────────────────────────────────────────────────────────
    # Cross-store assembled context (mirrors RetrievalHelper.search)
    # ──────────────────────────────────────────────────────────────────
    async def search(self, query: str, *,
                     scope: RetrievalScope,
                     intent: ContextIntent | None = None,
                     k: int = 10) -> AssembledContext: ...
    """
    Delegates to OntologyContextLoader.load with anchors resolved from the query.
    Returns the full assembled context (cards + bundles + lineage neighborhood + warnings).
    """

    # ──────────────────────────────────────────────────────────────────
    # Lineage / claim / code_list (new)
    # ──────────────────────────────────────────────────────────────────
    async def get_lineage(self, *, asset_rk: str,
                          direction: str = "both",
                          edge_kinds: list[str] | None = None,
                          max_hops: int = 1) -> LineageGraph: ...
    async def get_claims(self, *, asset_rk: str | None = None,
                         subject_ref: str | None = None,
                         object_ref: str | None = None,
                         min_confidence: float = 0.0) -> list[ClaimView]: ...
    async def get_code_lists(self, *, column_rk: str) -> list[CodeListView]: ...
```

Every method takes `RetrievalScope`. No `project_id` parameter on the new surface; the shim is in `RetrievalScope.for_project()`.

### 4.1 Output shapes

```python
@dataclass
class TableContext:
    asset_rk: str
    asset_kind: str
    name: str
    score: float
    schema_rk: str
    columns: list[ColumnInfo]
    concepts: list[str]
    key_areas: list[str]
    causal_relations: list[str]
    effective_sensitivity_class: str | None
    description: str | None
    primary_object_type: str | None        # derived from concepts[0]

@dataclass
class SqlPairHit:
    sql_pair_id: str
    question: str
    sql: str
    instructions: str | None
    references_asset_rks: list[str]
    score: float
    source_provenance: str                 # 'authored' | 'historical_q' | 'imported_legacy'

@dataclass
class InstructionHit:
    instruction_id: str
    title: str
    body: str
    scope_concepts: list[str]
    scope_key_areas: list[str]
    score: float
    source: str                            # 'card:instruction' | 'legacy_instruction_service'

@dataclass
class HistoricalQAHit:
    qa_id: str
    question: str
    answer_summary: str
    cited_asset_rks: list[str]
    satisfaction: float | None
    asked_at: datetime
    score: float
```

`SchemaContext`, `ViewBundle`, `MetricBundle`, `FunctionBundle` are thin wrappers around `AssetBundle` from the consumer spec.

---

## 5. Concrete retrievers — implementation outline

Each retriever is small (~100–300 LOC), focused, and mostly delegates to BundleStore or CardStore. The heavy lifting is in those layers; retrievers are interface adapters + scope resolution + result shaping.

### 5.1 `AssetRetrieval`

```python
class AssetRetrieval:
    def __init__(self, bundle_store: BundleStore, embedder: Any,
                 default_k: int = 5,
                 model_name: str = "gpt-4o-mini",
                 enable_column_selection_llm: bool = False):
        ...

    async def search(self, query: str, *, scope: RetrievalScope,
                     asset_kinds: list[str] | None = None,
                     k: int | None = None) -> list[TableContext]:
        """Vector + payload filter against hier_t4_assets_<env>."""
        hits = self.bundle_store.search_assets(
            query=query,
            org_id=scope.org_id,
            asset_kind=asset_kinds or scope.asset_kinds,
            filters=AssetSearchFilters(
                concepts=scope.concepts,
                key_areas=scope.key_areas,
                causal_relations=scope.causal_relations,
                source_id_in=scope.source_ids,
                lifecycle_stage_in=scope.lifecycle_stages,
                effective_sensitivity_class_lte=scope.sensitivity_max,
            ),
            k=k or self.default_k,
        )

        # Optional: LLM column selection for narrow-scope queries (preserves old behavior)
        if self.enable_column_selection_llm and len(hits) <= 3:
            return await self._llm_select_columns(query, hits)

        return [self._to_table_context(h) for h in hits]
```

Notes:
- The LLM column selection from old `TableRetrieval` (~3K LOC of helpers) is retained as **opt-in** via `enable_column_selection_llm`. Default is off — the new ranking is good enough for most cases without LLM-in-the-loop.
- For large-fanout searches (k > 10), no LLM; vector ranking + payload filter is the cheap path.

### 5.2 `CardRetrieval`

```python
class CardRetrieval:
    def __init__(self, card_store: CardStore, embedder: Any, default_k: int = 10):
        ...

    async def search(self, query: str, *,
                     scope: RetrievalScope,
                     kinds: list[str] | None = None,
                     markings: list[str] | None = None,
                     k: int | None = None) -> list[CardHit]:
        return self.card_store.search(
            query=query,
            tenant_id=scope.org_id,
            kind=kinds,
            markings=markings,
            k=k or self.default_k,
        )

    async def get_refs(self, *, card_id: str, kind: str,
                       direction: Literal['out', 'in'] = 'out') -> list[CardRef]:
        return self.card_store.list_refs(tenant_id=..., kind=kind, id=card_id, direction=direction)
```

### 5.3 `SqlPairsRetrievalV2`

Sql pairs are not first-class in the prior specs. This retriever introduces them.

**Storage:**
- New Qdrant collection `sql_pairs_<tenant>`.
- Point id: `sha256(normalized_question)`.
- Vector: embedding of the question (and optionally the SQL).
- Payload: `{ sql_pair_id, question_norm, references_asset_rks[], concepts[], key_areas[],
             source_provenance, authored_by, authored_at, valid_for_lifecycle }`.

- New Postgres mirror `sql_pair`:
  ```sql
  CREATE TABLE sql_pair (
    sql_pair_id    text PRIMARY KEY,
    org_id         text NOT NULL,
    question       text NOT NULL,
    sql            text NOT NULL,
    instructions   text,
    references_asset_rks  text[] NOT NULL DEFAULT '{}',
    concepts       text[] NOT NULL DEFAULT '{}',
    key_areas      text[] NOT NULL DEFAULT '{}',
    source_provenance text NOT NULL,                  -- 'authored' | 'historical_q' | 'imported_legacy'
    authored_by    text,
    authored_at    timestamptz NOT NULL DEFAULT now(),
    last_verified_at timestamptz,
    valid_for_lifecycle text NOT NULL DEFAULT 'production'
  );
  ```

**Filters applied:**
- `org_id` (always).
- `concepts && scope.concepts`, `key_areas && scope.key_areas`, `references_asset_rks ⊂ scope_assets`.
- Optionally `valid_for_lifecycle != 'deprecated'`.

**Source of sql_pairs:**
- Imported from legacy `sql_meta/<project>/sql_pairs.json` files (one-time migration; `source_provenance='imported_legacy'`).
- Newly authored via a separate UI / API.
- Promoted from `HistoricalQARetrieval` hits with high satisfaction (a closed-loop quality flywheel).

### 5.4 `InstructionsRetrievalV2`

Instructions in the legacy module are project-scoped free-form text. In the new model, instructions live in two places:

1. **Card kind `instruction`** (new) — for instructions tied to ontology concepts. Author writes a card with `kind: instruction`, frontmatter declares `applies_to_concepts[]` / `applies_to_key_areas[]` / `applies_to_causal_relations[]`. Body is the instruction text.

2. **Legacy `InstructionService`** — kept; for instructions that don't fit the card model (broad org-wide directives, deprecated patterns).

```python
class InstructionsRetrievalV2:
    async def search(self, query: str, *,
                     scope: RetrievalScope,
                     similarity_threshold: float = 0.1,
                     k: int = 30) -> list[InstructionHit]:
        # 1. Card-based: search cards_<tenant> with kind=instruction filtered by scope
        card_hits = self.card_store.search(
            query, tenant_id=scope.org_id, kind=['instruction'],
            extra_filters={
                'applies_to_concepts && %s' % scope.concepts if scope.concepts else None,
                ...
            },
            k=k,
        )

        # 2. Legacy: pull from InstructionService when legacy_project_id is set
        legacy_hits = []
        if scope.legacy_project_id and self.legacy_service:
            legacy_hits = await self.legacy_service.list_instructions(
                domain_id=scope.legacy_project_id,
            )

        return self._merge_and_dedupe(card_hits, legacy_hits, k=k)
```

Adding `instruction` as a card kind is a small extension to `semantic_layer_card_spec.md` §3.1 (the reserved-kinds list already mentions it). Promoting from reserved → live requires the per-kind frontmatter fields:

```yaml
---
id: phi_field_access_logging
layer: semantic
kind: instruction
version: 1
applies_to_concepts: [patient, encounter]
applies_to_key_areas: [HIPAA, Clinical_Operations]
markings: []
---
PHI field access must be logged with the requesting user_id, timestamp, and
purpose. ...
```

### 5.5 `HistoricalQARetrievalV2`

```python
# New Postgres + Qdrant for historical Q&A
CREATE TABLE historical_qa (
  qa_id              text PRIMARY KEY,
  org_id             text NOT NULL,
  asked_by           text NOT NULL,
  asked_at           timestamptz NOT NULL,
  question           text NOT NULL,
  answer_summary     text NOT NULL,
  full_answer        text,
  cited_asset_rks    text[] NOT NULL DEFAULT '{}',
  used_intent        text,                        -- ContextIntent
  used_anchors       text[] NOT NULL DEFAULT '{}',
  satisfaction       real,                        -- 0..1 from user feedback
  feedback           text
);
```

Qdrant: `historical_qa_<tenant>`, embedding of the question, payload includes `asset_rks` for scope filtering.

Triggered population: every `ask` tool call (from `mcp_qa_agents_spec`) writes a row at completion. Satisfaction recorded when feedback arrives via a separate API.

### 5.6 `LineageRetrievalV2`

```python
class LineageRetrievalV2:
    async def trace(self, *, asset_rk: str,
                    direction: str = 'both',
                    edge_kinds: list[str] | None = None,
                    max_hops: int = 1) -> LineageGraph:
        return self.bundle_store.lineage(
            asset_rk=asset_rk,
            direction=direction,
            edge_kinds=edge_kinds,
            max_hops=max_hops,
        )
```

Thin pass-through to BundleStore.

### 5.7 `ClaimRetrievalV2`

```python
class ClaimRetrievalV2:
    async def by_asset(self, *, asset_rk: str, min_confidence: float = 0.5) -> list[ClaimView]:
        bundle = self.bundle_store.get_bundle(asset_rk, include_concerns=[BundleConcern.CAUSAL])
        return [c for c in bundle.causal.get('claims', []) if c['confidence'] >= min_confidence]

    async def by_subject(self, *, subject_ref: str, ...) -> list[ClaimView]: ...
    async def by_object(self,  *, object_ref:  str, ...) -> list[ClaimView]: ...
```

### 5.8 `CodeListRetrievalV2`

```python
class CodeListRetrievalV2:
    async def for_column(self, *, column_rk: str) -> list[CodeListView]: ...
    async def search(self, query: str, *, scope: RetrievalScope) -> list[CodeListView]: ...
```

### 5.9 `SchemaPruning` (replaces `PreprocessSqlData`)

Old `PreprocessSqlData` truncates and prunes schemas to fit token budgets. The new equivalent:

```python
class SchemaPruning:
    def __init__(self, max_tokens: int = 100_000, model: str = "gpt-4o-mini"):
        ...

    def prune(self, schemas: list[SchemaContext], *,
              query: str | None = None,
              token_budget: int | None = None,
              prefer_concepts: list[str] | None = None,
              prefer_key_areas: list[str] | None = None) -> list[SchemaContext]:
        """
        Reduce schemas to fit token_budget. Strategy:
          1. Drop columns NOT in concept/key_area scope first.
          2. Drop sample values + statistics second.
          3. Truncate descriptions third.
          4. Drop entire low-relevance tables last.
        Relevance ranking uses bundle.score + concept-match count.
        """
```

The old `_truncate_ddl`, `_prune_schemas_intelligently`, `_calculate_relevance_score` helpers from `retrieval_helper.py` carry over conceptually, but use concept/key_area scope rather than blind structural relevance.

---

## 6. Backward-compat adapter

For caller sites that can't switch immediately (downstream services in CSOD workflows, dashboard recommender, etc.), a thin adapter exposes the old `RetrievalHelper` surface and delegates to `RetrievalHelperV2`:

```python
class RetrievalHelperLegacyAdapter:
    """Exposes the old RetrievalHelper API; delegates to RetrievalHelperV2."""

    def __init__(self, v2: RetrievalHelperV2):
        self._v2 = v2

    async def get_sql_pairs(self, query: str, project_id: str = None, **kwargs):
        scope = RetrievalScope.for_project(project_id, org_id=self._resolve_org(project_id))
        hits = await self._v2.get_sql_pairs(query=query, scope=scope, **kwargs)
        # Wrap in the old { 'documents': [...] } shape
        return {'documents': [self._to_legacy_shape(h) for h in hits]}

    # ... same pattern for each old method
```

Caller sites import `RetrievalHelperLegacyAdapter` instead of `RetrievalHelper`. No other change. Behavior identical for legacy project_id scopes (via translation table); new scope features ignored.

---

## 7. Migration path

### 7.1 Phase 0 — Land the new module (no caller changes)

- Build `ontology_foundry/consumer/retrieval/` per §2 layout.
- Implement all retrievers per §5.
- Ship `RetrievalHelperLegacyAdapter` per §6.
- Run unit tests against fixture tenants.
- **No caller site changes yet.** v1 retrieval continues serving production.

### 7.2 Phase 1 — Bootstrap legacy_project_translation

Per-tenant migration:
1. For each existing `sql_meta/<project>/project_metadata.json`, derive `(concepts, key_areas, source_ids)`:
   - Concepts: union of `mdl.concepts[]` across the project's tables (post-bottoms-up enrichment).
   - Key_areas: union of `mdl.key_areas[]`.
   - Source_ids: union of `mdl.source_id` values.
2. Write the row to `legacy_project_translation`.
3. Spot-check translations with a handful of representative queries.

Sql_pair migration:
- Per `sql_meta/<project>/sql_pairs.json`, insert into the new `sql_pair` table with `source_provenance='imported_legacy'`.
- Compute `references_asset_rks` by parsing the SQL with sqlglot and matching table names to existing assets.
- Failures (asset not found) are logged; the row still imports but loses scope precision.

### 7.3 Phase 2 — Swap caller imports

Per caller subsystem, in order of safety:
1. CSOD workflow tests — replace `from app.agents.retrieval import RetrievalHelper` with `from ontology_foundry.consumer.retrieval import RetrievalHelperLegacyAdapter as RetrievalHelper`.
2. Validate against the curated eval corpus (`evaluation_harness_spec.md`).
3. If parity holds, ship.
4. Repeat for next subsystem.

### 7.4 Phase 3 — Deprecate `project_id` at caller sites

Once all callers run on the adapter:
1. Identify call sites still passing `project_id`.
2. Replace with explicit `RetrievalScope(concepts=..., key_areas=...)` calls.
3. After all sites are updated, the adapter's `project_id` parameter is marked deprecated.

### 7.5 Phase 4 — Retire the legacy module

When usage telemetry shows zero `legacy_project_id` calls for one release cycle:
1. Remove `RetrievalHelperLegacyAdapter`.
2. Remove `genieml/agents/app/agents/retrieval/` (the old module).
3. Remove `legacy_project_translation` table.

---

## 8. The "do we need a new version" question, answered

Short answer: **yes, but it's a small new version**, not a rewrite.

What's actually new code:
- ~10 retriever files at ~200–400 LOC each ≈ 3K LOC.
- One Postgres table (`sql_pair`), one optional one (`historical_qa`), one translation table.
- Two new Qdrant collections (`sql_pairs_<tenant>`, `historical_qa_<tenant>`).
- One new card kind (`instruction`) — small frontmatter extension.

What's reused unchanged:
- BundleStore + CardStore + OntologyContextLoader (the heavy machinery; specced already).
- Embedding stack (text-embedding-3-small).
- The old `PreprocessSqlData`'s pruning heuristics (carried over conceptually into `SchemaPruning`).
- The old `TableRetrieval`'s LLM-column-selection path (preserved as opt-in).

What's *less* code than the old module:
- ~3,000 LOC of `retrieval.py` + `retrieval_helper.py` shrinks because the storage abstraction is properly factored out into BundleStore. Most retrievers become 100–300 LOC façades. Total new module: ~3K LOC vs old ~15K LOC.

---

## 9. Test plan

| Test | Verifies |
|---|---|
| `test_scope_for_project_translation` | `RetrievalScope.for_project(p_id)` resolves to expected `(concepts, key_areas, source_ids)` for fixture tenants |
| `test_asset_retrieval_filters_by_concepts` | Query against tenant with mixed-concept assets returns only matching concept |
| `test_asset_retrieval_filters_by_sensitivity_max` | Role-scoped retrieval omits assets above `sensitivity_max` |
| `test_card_retrieval_by_kind` | Filtering by `kind=['causal_node']` returns only causal nodes |
| `test_sql_pairs_imported_legacy` | Imported sql_pairs hit with correct `references_asset_rks` populated |
| `test_sql_pairs_new_collection_isolation` | sql_pairs from tenant A don't surface in tenant B searches |
| `test_instructions_card_plus_legacy_merge` | InstructionsRetrievalV2 returns both card-sourced and legacy-service-sourced, deduped |
| `test_historical_qa_population` | After an `ask` call, `historical_qa` row exists and is retrievable |
| `test_lineage_max_hops` | Lineage trace at `max_hops=1` doesn't return 2-hop neighbors |
| `test_legacy_adapter_parity` | For the same `(query, project_id)` input, adapter returns documents whose ids/scores match the old `RetrievalHelper` within tolerance |
| `test_schema_pruning_concept_aware` | Pruning preserves columns matching `prefer_concepts` over others when over budget |
| `test_search_assembled_context` | `search(query, scope, intent)` returns an `AssembledContext` with non-zero `cards_full` and `bundles_loaded` |

### 9.1 Parity test against eval harness

A specific test mode runs the same curated CSOD question corpus (from `evaluation_harness_spec.md` §2) through:
- `RetrievalHelper` (legacy, against legacy Chroma stores).
- `RetrievalHelperV2` with `legacy_project_id` set (translation path).
- `RetrievalHelperV2` with explicit `(concepts, key_areas)` (native path).

Pass criterion: native-path Context Sufficiency ≥ legacy Context Sufficiency – 0.02.

---

## 10. Open items

- **Per-call cost telemetry** — every retrieval call should record token cost (when LLM-in-the-loop) and DB query cost. Defer to the observability spec.
- **Caching layer** — `RetrievalHelperV2` results are cacheable by `(method, args_hash, scope_hash, last_sync_event_id)`. Defer to operational tuning.
- **Streaming hits** — for large `k`, retrievers could stream hits to callers. Not needed v1; defer.
- **Multi-language retrieval** — when tenant supports multiple languages, do we issue multilingual queries? Defer; current path is English-only at the embedding level.
- **Card kind `instruction`** — promotion from reserved to active needs a one-line update in `semantic_layer_card_spec.md` §3.1. Apply when this spec implementation lands.
- **Reuse of `RelationshipRecommendation` and `SemanticsDescription`** from dataservices — both could feed the asset retrieval path (relationship-aware ranking, semantic-unit filters). Defer to second wave.

---

## 11. Cross-spec amendments (deferred)

| Spec | Section | Change |
|---|---|---|
| `semantic_layer_card_spec.md` | §3.1 | Promote `instruction` from reserved to live; add `applies_to_concepts`, `applies_to_key_areas`, `applies_to_causal_relations` frontmatter fields. |
| `bundle_consumer_api_spec.md` | §2 | Add `sensitivity_max` filter to `list_assets` filters (used by RetrievalScope). |
| `mcp_qa_agents_spec.md` | §3 | MCP tools route through `RetrievalHelperV2` for unified behavior (currently described as using `BundleStore` directly — reframe). |
| `evaluation_harness_spec.md` | §3 | Add parity test (§9.1 of this spec) to the harness. |

Apply when implementation lands.

---

## 12. Change log

| Date | Change |
|---|---|
| 2026-05-17 | Initial draft. |
