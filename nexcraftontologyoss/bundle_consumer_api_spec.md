# Bundle Consumer API — Specification

**Status:** Draft 2026-05-15.
**Part of:** Data Knowledge Hierarchy series.
**Depends on:** `mdl_bundle_spec.md`, `semantic_layer_card_spec.md`, `T2_to_T6_amundsenrds_sidecar_spec.md`, `hierarchy_persistence_and_ingestion_spec.md`.
**Leverages:** `ontology_foundry.retrieval.RetrievalAgent` pattern (the shape consumers expect for hits + scores); `ontology_foundry.models.RetrievalHit` (re-used in consumer responses).

---

## 1. Scope

This spec defines the **read-side contract** for internal foundry consumers — primarily the Compliance skill, the dashboard recommender, the metric proposer, and any future skill that needs to reason over the hierarchy.

The contract has two layers:

1. **`BundleStore`** — low-level access to per-asset bundles by id, by filter, or by semantic search. Surfaces full bundles or partial views.
2. **`OntologyContextLoader`** — higher-level convenience that composes bundle reads with card-aware traversal and the graduated-detail policy. This is what most skills should use.

Out of scope:
- The bundle file formats (in `mdl_bundle_spec.md`).
- Card storage (in `semantic_layer_card_spec.md`).
- External publishers (in `bundle_publishers_spec.md`).

---

## 2. `BundleStore` Protocol

```python
class BundleStore(Protocol):

    # ---- direct reads ----
    def get_bundle(self, asset_rk: str, *,
                   include_concerns: list[BundleConcern] | None = None,
                   include_manifest: bool = True) -> AssetBundle: ...
    """
    Read the full bundle (or a subset of concerns) for one asset.
    If include_concerns is None, all six (mdl/context/bindings/governance/causal/metrics) are loaded.
    """

    def get_catalog_bundle(self, catalog_uid: str) -> CatalogBundle: ...
    """Read catalog.json + catalog_assets_index.json."""

    def get_manifest(self, asset_rk: str) -> BundleManifest: ...
    """Cheap: read just bundle_manifest.json. Useful for staleness checks."""

    # ---- listing ----
    def list_assets(self, *,
                    org_id: str | None = None,
                    source_id: str | None = None,
                    catalog_uid: str | None = None,
                    schema_rk: str | None = None,
                    asset_kind: str | list[str] | None = None,
                    lifecycle_stage: str | list[str] | None = None,
                    domain_tags: list[str] | None = None,        # ANY
                    domain_tags_all: list[str] | None = None,    # ALL
                    canonical_entity: str | None = None,
                    compliance_regime: str | None = None,
                    sensitivity_class: str | None = None,
                    contains_pii: bool | None = None,
                    cursor: str | None = None,
                    limit: int = 100) -> AssetPage: ...
    """
    Filtered enumeration. Backed by Postgres queries against amundsenrds + sidecars
    + v_asset_effective. Returns paged results with stable cursor.
    """

    # ---- semantic search ----
    def search_assets(self, query: str, *,
                      org_id: str | None = None,
                      asset_kind: str | list[str] | None = None,
                      filters: AssetSearchFilters | None = None,
                      k: int = 10,
                      min_score: float | None = None) -> list[AssetHit]: ...
    """
    Qdrant-backed semantic search over T4 collection with payload filters.
    Returns ranked AssetHits with scores; consumer decides whether to fetch full bundles.
    """

    def search_fields(self, query: str, *,
                      asset_rk: str | None = None,
                      field_kind: str | list[str] | None = None,
                      filters: FieldSearchFilters | None = None,
                      k: int = 10) -> list[FieldHit]: ...
    """Qdrant-backed search over T5 collection."""

    def search_cards(self, query: str, *,
                     tenant_id: str,
                     kind: str | list[str] | None = None,
                     markings: list[str] | None = None,
                     k: int = 10) -> list[CardHit]: ...

    # ---- traversal ----
    def lineage(self, *, asset_rk: str,
                direction: Literal["upstream", "downstream", "both"] = "both",
                edge_kinds: list[str] | None = None,
                max_hops: int = 1) -> LineageGraph: ...

    def neighbors(self, *, asset_rk: str,
                  hops: int = 1,
                  via: list[NeighborKind] | None = None) -> NeighborGraph: ...
    """
    Generic graph neighborhood:
      via = ['lineage_edge', 'shares_canonical_entity', 'in_same_schema',
             'shares_equivalence_class', 'card_refs']
    """

    # ---- staleness / versioning ----
    def is_stale(self, asset_rk: str) -> StalenessReport: ...
    """
    Compare bundle_emit_state.last_inputs_hash against current storage state.
    Returns 'fresh' | 'stale' | 'unknown'.
    """

    def regenerate(self, asset_rk: str, *,
                   actor: str = "consumer-request") -> None: ...
    """
    Force-regenerate the bundle. Synchronous wrapper around the queue task.
    Use sparingly; prefer staleness check + accept stale.
    """
```

### 2.1 `BundleConcern` enum

```python
class BundleConcern(StrEnum):
    MDL       = "mdl"
    CONTEXT   = "context"
    BINDINGS  = "semantic_bindings"
    GOVERNANCE = "governance"
    CAUSAL    = "causal"
    METRICS   = "metrics"
```

Selective loading lets consumers avoid pulling causal claims (potentially large) when they only need MDL + bindings.

### 2.2 `AssetBundle` shape

```python
@dataclass
class AssetBundle:
    asset_rk: str
    asset_kind: str
    manifest: BundleManifest
    mdl: dict | None              # parsed mdl.json
    context: dict | None
    semantic_bindings: dict | None
    governance: dict | None
    causal: dict | None
    metrics: dict | None
    stale: bool                   # True if storage state has changed since emission
    materialized_from: Literal["disk", "regenerated_on_read"]
```

`materialized_from` records whether the bundle came from the on-disk artifact or was regenerated transiently because the disk version was stale and the caller asked for fresh.

### 2.3 `AssetHit`

```python
@dataclass
class AssetHit:
    asset_rk: str
    asset_kind: str
    score: float
    payload: dict[str, Any]       # the Qdrant payload (kind, lifecycle_stage, domain_tags, ...)
    snippet: str | None           # short text excerpt that matched

    def load(self, store: BundleStore, *,
             include_concerns: list[BundleConcern] | None = None) -> AssetBundle:
        return store.get_bundle(self.asset_rk, include_concerns=include_concerns)
```

Two-step pattern: search returns lightweight hits; consumer chooses which to materialize into full bundles. Standard for cost-aware retrieval.

### 2.4 Pagination

`list_assets` returns:

```python
@dataclass
class AssetPage:
    items: list[AssetSummary]
    cursor: str | None            # opaque; pass to next call
    total_estimate: int | None    # may be None for very large filters
```

Cursors are stable across writes (anchored on `rk` + creation order). Consumers can resume after restart.

---

## 3. Materialization strategy

### 3.1 Disk-first

Default reads pull bundle files from disk. The path is `tenants/<org_id>/assets/<source_id>/<schema>/<asset_name>/<concern>.json`.

### 3.2 Staleness check

`BundleStore` compares `bundle_emit_state.last_inputs_hash` against current row hashes in storage. If stale and the caller requested *fresh*:

```python
bundle = store.get_bundle(rk, include_concerns=[BundleConcern.GOVERNANCE], fresh=True)
# triggers a synchronous regenerate of just the governance concern,
# or full re-emit if multiple concerns are stale
```

Default `fresh=False`: consumers accept the on-disk version, accepting possible staleness; faster.

### 3.3 In-memory cache

A per-process LRU cache keyed by `(asset_rk, manifest_sha256)`. Cache size configurable; default 1000 entries. Cache is bypassed when `fresh=True`.

### 3.4 Fallback to direct storage query

If the bundle file is missing (rare; should only happen for newly-created assets before the first emission), `BundleStore` falls back to building the bundle on-the-fly from Postgres + card reads, returns it with `materialized_from='regenerated_on_read'`, and enqueues a real emission.

---

## 4. `OntologyContextLoader` — higher-level

Most skills don't want to think about lineage hops, card refs, and concern selection separately. The `OntologyContextLoader` composes those into a single call shaped around the graduated-detail policy (depth budget × edge filter × branching guard, per prior design discussion).

```python
class OntologyContextLoader:
    def __init__(self,
                 *,
                 bundle_store: BundleStore,
                 card_store: CardStore,
                 default_policy: ContextPolicy):
        ...

    def load(self,
             *,
             anchors: list[AssetAnchor | CardAnchor],
             intent: ContextIntent,
             policy: ContextPolicy | None = None) -> AssembledContext: ...
```

### 4.1 `ContextPolicy`

```python
@dataclass
class ContextPolicy:
    max_hops_full: int = 1                       # cards at distance ≤ 1 → full body
    max_hops_summary: int = 2                    # cards at distance 2 → frontmatter + first paragraph
    max_hops_manifest: int = 3                   # cards at distance 3 → id + title + one-line summary
    branching_cap: int = 15                      # demote-to-summary if neighborhood expands beyond this
    token_budget: int | None = 16_000            # hard ceiling; demotes further to fit
    edge_filter: EdgeFilter | None = None        # which traversal edges count
    include_bundle_concerns: list[BundleConcern] = field(default_factory=lambda: [
        BundleConcern.MDL, BundleConcern.CONTEXT,
        BundleConcern.BINDINGS, BundleConcern.GOVERNANCE,
    ])
    include_causal: bool = False                 # opt-in; high-churn data
    include_metrics: bool = False                # opt-in
```

The defaults match the graduated-detail recipe from the design conversation.

### 4.2 `ContextIntent`

```python
class ContextIntent(StrEnum):
    CAUSAL_REASONING       = "causal_reasoning"        # traverses object_type → causal_node → object_type
    GOVERNANCE_LOOKUP      = "governance_lookup"       # traverses markings + asset_owner + sensitivity inheritance
    COMPLIANCE_REC         = "compliance_rec"          # traverses compliance_regime → bound assets → causal nodes
    DASHBOARD_REC          = "dashboard_rec"           # metric-anchored; traverses metric → primary_asset → dimensions
    SCHEMA_LOOKUP          = "schema_lookup"           # narrow: asset MDL + bindings
    ENTITY_RESOLUTION      = "entity_resolution"       # equivalence classes + canonical entity bindings
```

Each intent maps to a recipe — a preset edge filter + policy adjustments. Recipes live in `ontology_foundry/consumer/recipes.py` and are extensible. A consumer can override with explicit `policy=...`.

### 4.3 `AssembledContext`

```python
@dataclass
class AssembledContext:
    intent: ContextIntent
    anchors: list[ResolvedAnchor]

    # cards
    cards_full: list[CardView]                  # distance 0 + ≤ max_hops_full
    cards_summary: list[CardSummary]            # distance 2
    cards_manifest: list[CardManifestEntry]     # distance 3 (id + title + one-line)

    # bundles
    bundles: dict[str, AssetBundle]             # keyed by asset_rk

    # token accounting
    estimated_tokens: int
    demotions_applied: list[str]                # e.g., "branching cap hit at hop 1; demoted 4 cards to summary"

    def render_prompt(self) -> str: ...
    """Concatenate into a single LLM-ready string with section headers."""
```

`render_prompt()` emits a deterministic, audit-friendly prompt block. Tests assert byte-equivalence for the same context.

### 4.4 Algorithm sketch

```
1. Resolve anchors (asset_rk or card id) to a starting set of (kind, id) nodes.
2. Pull each anchor's full bundle (per include_bundle_concerns).
3. For each anchor card or bound card from bundles[i].semantic_bindings.primary_object_type:
     BFS over card refs respecting edge_filter; record (id, distance).
4. Apply branching guard: at each hop, if frontier > branching_cap,
   keep top-k by relevance (Qdrant similarity to anchor query) + always keep
   anchor's direct lineage cards.
5. Materialize card content at appropriate detail tier per distance.
6. If estimated_tokens > policy.token_budget, demote farthest tier first
   (manifest stays; summary may drop to manifest; full may drop to summary).
7. Assemble and return.
```

### 4.5 Telemetry

Every `load()` call emits a structured event:

```json
{
  "intent": "causal_reasoning",
  "anchors": ["snowflake://.../encounters"],
  "policy_used": {...},
  "cards_full_count": 4,
  "cards_summary_count": 6,
  "cards_manifest_count": 11,
  "bundles_loaded": 3,
  "estimated_tokens": 12420,
  "demotions_applied": ["branching cap hit at hop 1; demoted 2 cards"],
  "wall_time_ms": 184
}
```

These power the eval harness (`evaluation_harness_spec.md`) — Context Sufficiency and Token Efficiency metrics consume them directly.

---

## 5. Intent recipes (defaults)

### 5.1 `CAUSAL_REASONING`

```python
ContextPolicy(
    max_hops_full=1,
    max_hops_summary=3,                          # causal chains can run a hop longer
    max_hops_manifest=4,
    include_bundle_concerns=[
        BundleConcern.MDL, BundleConcern.CONTEXT,
        BundleConcern.BINDINGS, BundleConcern.CAUSAL,
    ],
    include_causal=True,
    edge_filter=EdgeFilter(card_kinds=['object_type', 'causal_node', 'derived_state'],
                           card_ref_fields=['refs', 'subject_refs', 'outcome_refs', 'attaches_to']),
)
```

### 5.2 `GOVERNANCE_LOOKUP`

```python
ContextPolicy(
    max_hops_full=1,
    max_hops_summary=2,
    include_bundle_concerns=[
        BundleConcern.CONTEXT, BundleConcern.GOVERNANCE,
        BundleConcern.BINDINGS,                  # bindings drive marking propagation
    ],
    edge_filter=EdgeFilter(card_kinds=['object_type', 'interface'],
                           card_ref_fields=['extends'],
                           marking_aware=True),  # follow marking propagation chains
)
```

### 5.3 `COMPLIANCE_REC`

```python
ContextPolicy(
    max_hops_full=2,                             # compliance reasoning ranges wider
    max_hops_summary=3,
    branching_cap=25,                            # higher; compliance touches many assets
    include_bundle_concerns=[
        BundleConcern.MDL, BundleConcern.CONTEXT,
        BundleConcern.BINDINGS, BundleConcern.GOVERNANCE,
        BundleConcern.CAUSAL,
    ],
    include_causal=True,
    include_metrics=True,
    edge_filter=EdgeFilter(card_kinds=['object_type', 'causal_node', 'metric'],
                           include_compliance_regime_assets=True),
)
```

### 5.4 `DASHBOARD_REC`

Anchored on metrics. Pulls the metric's primary asset + its dimensions' assets, the bound object_types, and any causal_nodes the metric is a leading indicator of.

### 5.5 `SCHEMA_LOOKUP`

The narrow case — single asset, no traversal. Cheap.

```python
ContextPolicy(
    max_hops_full=0,
    max_hops_summary=0,
    max_hops_manifest=0,
    include_bundle_concerns=[BundleConcern.MDL, BundleConcern.BINDINGS],
)
```

### 5.6 `ENTITY_RESOLUTION`

Anchored on canonical_entity or equivalence_class. Pulls every bound asset + the binding details.

---

## 6. Examples

### 6.1 Compliance skill walking PHI-bearing assets

```python
loader = OntologyContextLoader(bundle_store=store, card_store=cards, default_policy=...)

# Stage 1: enumerate PHI-bearing assets in scope
phi_assets = store.list_assets(
    org_id="acme-corp",
    contains_pii=True,
    compliance_regime="HIPAA",
    lifecycle_stage="production",
    limit=200,
)

# Stage 2: for the top-priority subset, load full compliance context
for asset in phi_assets.items[:25]:
    ctx = loader.load(
        anchors=[AssetAnchor(rk=asset.rk)],
        intent=ContextIntent.COMPLIANCE_REC,
    )
    # ctx.render_prompt() goes into the LLM that produces dashboard recommendations
```

### 6.2 Causal-question routing

```python
hits = store.search_assets("how does training affect attrition", k=5)
top = hits[0]

ctx = loader.load(
    anchors=[AssetAnchor(rk=top.asset_rk)],
    intent=ContextIntent.CAUSAL_REASONING,
)

# ctx contains: anchor asset's bundle + employee/training_assignment cards (full),
# overdue_risk/compliance_gap causal_node cards (full or summary depending on distance),
# any drift_flag warnings (surfaced separately in ctx.warnings)
```

### 6.3 Cross-source entity resolution

```python
ctx = loader.load(
    anchors=[CardAnchor(card_id="employee", kind="object_type")],
    intent=ContextIntent.ENTITY_RESOLUTION,
)

# ctx.bundles contains all assets bound to 'employee' across sources;
# ctx.cards_full contains the employee card + its interfaces (trainable, auditable);
# ctx.cards_summary contains causal_nodes the employee participates in.
```

---

## 7. Asset-anchor vs card-anchor

```python
@dataclass
class AssetAnchor:
    rk: str

@dataclass
class CardAnchor:
    card_id: str
    kind: str

ResolvedAnchor = AssetAnchor | CardAnchor
```

Both supported. Asset-anchored context is the common case ("tell me about this table"). Card-anchored context is used for ontology-first reasoning ("which assets implement Auditable?").

---

## 8. Warnings surface

Some conditions are not errors but should be communicated:

```python
@dataclass
class ContextWarning:
    kind: str                # 'bundle_stale' | 'binding_drift' | 'low_confidence_claims' | ...
    asset_rk: str | None
    card_id: str | None
    detail: str
```

`AssembledContext.warnings: list[ContextWarning]` is non-empty when:
- Any bundle in the assembled set is stale (`bundle_emit_state.last_inputs_hash` mismatch).
- Any bound card has an open `card_version_drift` flag.
- Causal claims with `confidence < 0.5` are included (and `include_causal=True`).
- A branching-cap demotion dropped cards with high pre-cap relevance scores.

Skills decide whether to surface warnings to users (Compliance: yes; routing: probably not).

---

## 9. Stable identifiers and versioning

### 9.1 `asset_rk` and `card_id` are stable

Consumers may cache bundle/context responses keyed by `asset_rk` + `manifest_sha256` (assets) or `card_id` + `version` (cards). Stable means: a rename produces a *new* rk/id, not a mutation of the old one.

### 9.2 Bundle manifest as the cache key

`bundle.manifest.bundle_manifest_version + asset_rk + per-file sha256` uniquely identifies a bundle's content state. Consumers' caches should key on `manifest_sha256` to invalidate on any concern's change.

### 9.3 API versioning

This API is at v1. Future versions:
- v1.x: additive only (new optional fields, new methods).
- v2: breaking changes get a new module path (`ontology_foundry.consumer.v2`); v1 stays available for a deprecation period.

---

## 10. Concurrency and consistency

### 10.1 Reads are unsynchronized

`BundleStore` reads do not lock. A consumer reading mid-regeneration sees either the old bundle or the new bundle (atomic rename guarantee from `hierarchy_persistence_and_ingestion_spec.md` §6.2), never a half-written one.

### 10.2 No transactional context across multiple bundles

Consumers loading many bundles do not get cross-bundle transactional consistency. A bundle for asset A and a bundle for asset B may have been emitted at different times. The `rendered_at` timestamp on each tells the consumer how recent each is.

### 10.3 Snapshot-equivalent reads (deferred)

For consumers that need a coherent snapshot across many assets (e.g., a compliance audit), a future enhancement adds `BundleStore.open_snapshot(at: datetime)` returning a snapshot reader. Not in v1.

---

## 11. Error handling

| Error | Cause | Behavior |
|---|---|---|
| `BundleNotFound` | rk does not exist in storage | Raised |
| `BundleEmissionInProgress` | rk exists but never emitted; emission queued | Returns partial bundle materialized from storage with `materialized_from='regenerated_on_read'`; logs the pending emission |
| `BundleStale` (with `fresh=True`) | Storage newer than disk; sync regen needed | Triggers regenerate; on failure, raises `RegenerationFailed` |
| `InvalidPolicy` | `ContextPolicy` self-inconsistent (e.g., max_hops_full > max_hops_summary) | Raised |
| `TokenBudgetExceeded` | All demotions applied but still over budget | Logged + raised; consumer must adjust policy or anchors |
| `CardNotFound` | Anchor card_id doesn't exist | Raised |
| `BindingDriftFatal` | Binding has an open drift flag AND `policy.strict_bindings=True` | Raised |

---

## 12. Performance targets

For a tenant with ~5,000 assets, ~500 cards:

| Operation | P50 | P95 |
|---|---|---|
| `get_bundle` (warm cache) | < 2 ms | < 10 ms |
| `get_bundle` (cold disk) | < 20 ms | < 100 ms |
| `list_assets` with filters (cursor page of 100) | < 100 ms | < 400 ms |
| `search_assets` (Qdrant, k=10) | < 80 ms | < 250 ms |
| `OntologyContextLoader.load` (intent=CAUSAL_REASONING, anchors=1) | < 300 ms | < 1.2 s |
| `OntologyContextLoader.load` (intent=COMPLIANCE_REC, anchors=1) | < 600 ms | < 2.5 s |

Measured by the eval harness's perf bench. Regressions fail CI.

---

## 13. Implementation locations

```
ontology_foundry/consumer/
  __init__.py
  bundle_store.py           # BundleStore Protocol + filesystem-backed impl
  context_loader.py         # OntologyContextLoader + recipes
  recipes.py                # ContextPolicy per ContextIntent
  errors.py
  cache.py                  # LRU + manifest-keyed
  models.py                 # AssetBundle, AssetHit, AssembledContext, ContextPolicy, ...
```

`BundleStore` impl reads bundles from disk, falls back to Postgres-derived emission on miss, and uses Qdrant for semantic search. `OntologyContextLoader` composes `BundleStore` + `CardStore` (from `semantic_layer_card_spec.md` §14).

---

## 14. Open items

- **Snapshot-coherent reads** (§10.3) — deferred.
- **Consumer-side row-level access control** — when the tenant has a governance profile that restricts certain users from seeing PHI columns, the consumer API needs an `actor` parameter and PII redaction in returned bundles. Defer to the governance-profile spec.
- **Streaming context loader** — for very large compliance audits, an iterator API that yields contexts asset-by-asset rather than materializing all in memory. Defer.
- **Cross-tenant access** — some platform skills may need cross-tenant aggregates (e.g., benchmark "your attrition risk vs peer orgs"). Strict isolation in v1; cross-tenant API deferred.

---

## 15. Change log

| Date | Change |
|---|---|
| 2026-05-15 | Initial draft. |
