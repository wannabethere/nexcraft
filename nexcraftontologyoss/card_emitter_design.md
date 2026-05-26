# Card Emitter Design

How structured artifacts from `ontology-foundry` get rendered into the
markdown-with-frontmatter "cards" defined in
[`ontology/causal_ontology_example_slice.md`](ontology/causal_ontology_example_slice.md).

The emitter is **external to the foundry**. The foundry stops at clean data
artifacts; the emitter knows the card grammar and lives in this repo.

---

## Why the split

- **Foundry** produces `RelationArtifact`, `RelationSchema`/`RelationType`,
  `CausalEdgeFinding`, `ClaimArtifact`, linked `EntitySpan`s. Pure data. No
  knowledge of YAML frontmatter, no versioning concerns, no card layer
  assignment.
- **Card emitter** maps those artifacts into card kinds, assigns layers, picks
  IDs, bumps versions, dedupes against existing cards on disk, and writes
  markdown files.

Keeping the card grammar out of the foundry means the foundry stays domain-
generic and the card format can evolve in this repo without breaking ingestion.

---

## Field correspondence — foundry artifacts → cards

### `RelationArtifact` → instance evidence under a `link_type` card

A `RelationArtifact` is one observed edge. Many of them aggregate into one
`link_type` card. The aggregator picks the canonical predicate (after schema
induction) and decides cardinality from the support distribution.

| Foundry field                          | Becomes in card                          |
|----------------------------------------|------------------------------------------|
| `predicate` (canonical, post-induction)| `id:` (snake_case)                       |
| `subject_type`                         | first entry in `refs:`                   |
| `object_type`                          | second entry in `refs:`                  |
| `confidence` (aggregated, e.g. mean)   | `confidence:`                            |
| `source` ends `:seeded` vs `:novel`    | informs `derivation:` choice             |
| `chunk_id` (per artifact)              | provenance — kept in evidence store, not card |
| `evidence_text` (per artifact)         | provenance — kept in evidence store, not card |

### `RelationType` (induced) → `link_type` card

| Foundry field   | Card frontmatter key   |
|-----------------|------------------------|
| `predicate`     | `id`                   |
| `domain`        | `refs[0]` (source object_type) |
| `range`         | `refs[1]` (target object_type) |
| (always)        | `layer: semantic`      |
| (always)        | `kind: link_type`      |
| n/a (emitter)   | `version` — bumped when fields change |
| n/a (emitter)   | `derivation` — `structural` if FK-anchored, `inferred` otherwise |
| n/a (emitter)   | `cardinality` — derived from support distribution of the underlying artifacts (1:1, 1:N, N:1, N:N) |

### Linked `EntitySpan` (collapsed by anchor) → `object_type` card

| Foundry field       | Card frontmatter key |
|---------------------|----------------------|
| `seed_anchor`       | `id`                 |
| `span_type`         | informs `extends:`, refs to base type |
| (aggregated refs)   | `refs:` — all predicates with this anchor as subject or object |
| n/a (emitter)       | `layer: semantic`, `kind: object_type` |
| n/a (emitter)       | `markings:` — added at this layer, not by the foundry |

### `ClaimArtifact(claim_type=causal)` → `causal_node` or `causal_edge` candidate

Causal claims from text feed the causal layer. The emitter promotes them to
draft cards; the existing `causal/` module's findings supply the statistical
metadata.

| Foundry field        | Card frontmatter key    |
|----------------------|-------------------------|
| `entity_refs`        | `refs:`                 |
| `text`               | card body (prose)       |
| `claim_type`         | `kind: causal_node` or `kind: causal_edge` (router decides) |
| `confidence`         | informs draft `weight.source` (`hypothesized` vs `learned`) |

### `CausalEdgeFinding` (from `ontology_foundry.causal`) → `causal_edge` card

| Foundry field        | Card frontmatter key    |
|----------------------|-------------------------|
| `source`             | `refs[0]`               |
| `target`             | `refs[1]`               |
| `algorithm`          | `weight.source` (`learned:pc`, `learned:lingam`, …) |
| `weight`             | `weight.value`          |
| `diagnostics`        | populates `weight.ci`, `weight.n` when present; otherwise stays in evidence store |
| n/a (emitter)        | `effect:` — `increases` / `decreases` from weight sign |
| n/a (emitter)        | `identifiability:`, `confounders:`, `functional_form:` — written by causal-analysis follow-up, not extracted at this stage |

---

## Emitter sketch

### Where it lives

```
nexcraftontologyoss/
├── ontology/                    # canonical cards (committed)
│   ├── object_types/
│   ├── link_types/
│   ├── causal_nodes/
│   ├── causal_edges/
│   └── …
└── tooling/
    └── card_emitter/            # this package
        ├── routers.py           # artifact → card kind
        ├── aggregators.py       # RelationArtifact → link_type (with cardinality inference)
        ├── renderers.py         # dict → frontmatter+body markdown
        ├── writer.py            # idempotent file writes with version bumps
        └── evidence_store.py    # per-artifact provenance (out-of-card)
```

### Inputs

A single `EmissionRequest` carries one ingestion run's output:

- `entities`: `list[EntitySpan]` (post-link, grouped by `seed_anchor`)
- `relations`: `list[RelationArtifact]`
- `relation_schema`: `RelationSchema` from `induce_schema(...)`
- `claims`: `list[ClaimArtifact]`
- `causal_findings`: `list[CausalEdgeFinding]`
- `existing_cards`: parsed cards already on disk (for diff / version bump)

### Pipeline

```
EmissionRequest
   │
   ├── aggregate relations by canonical predicate
   │       group RelationArtifacts by RelationType.predicate
   │       → one draft link_type card per group
   │       infer cardinality from (subject_ref, object_ref) support
   │
   ├── aggregate entities by anchor
   │       group EntitySpans by seed_anchor
   │       → one draft object_type card per anchor
   │       collect refs from inbound + outbound relations
   │
   ├── route causal artifacts
   │       claim(causal) → causal_node draft if entity is variable-like
   │                       causal_edge draft otherwise
   │       CausalEdgeFinding → fill in weight metadata on matching causal_edge
   │
   ├── diff against existing_cards
   │       if a draft is identical → skip
   │       if frontmatter changed → bump version, write
   │       if new → assign version 1, write
   │
   └── write evidence store rows
           one row per RelationArtifact / ClaimArtifact / finding
           keyed by (card_id, version, source_run_id)
```

### What the emitter does NOT do

- **Does not invent layer assignments.** `layer: semantic` is the default for
  the kinds emitted here. `kinetic` and `dynamic` cards (actions, rules, roles,
  markings, lineage) are not produced by extraction — they're authored by hand
  or by purpose-built generators outside this emitter.
- **Does not write `weight.ci` or `weight.n` from text claims.** Only the
  causal pipeline's findings can populate those. Text-sourced claims yield
  drafts with `weight.source: hypothesized`.
- **Does not pick `identifiability`, `confounders`, `functional_form`.** These
  require causal-analysis judgment and are filled in by a follow-up step (often
  human-reviewed).
- **Does not delete cards.** A draft that disappears in a new run means the
  evidence weakened — the card stays with a deprecation marker if applicable,
  but removal is a separate operation.
- **Does not modify cards in non-extracted layers.** A `link_type` card can
  reference a `marking:` from the dynamic layer, but the emitter never edits
  marking cards.

### Versioning

Cards carry `version: N`. The emitter's diff rule:

- Same `id`, identical frontmatter → no-op.
- Same `id`, frontmatter changed → version += 1, new prose if body regenerated,
  old card archived under `ontology/_history/<id>.v<N-1>.md`.
- New `id` → version 1.

Card bodies (the prose) are regenerated on every meaningful change. Their
input is the structured frontmatter plus any newly-added evidence; an LLM
templating call produces the prose, validated to ensure every claim in it
appears in the frontmatter or in a referenced evidence row.

### Evidence store

Provenance does not live in cards. A separate evidence store
(`ontology/_evidence/<card_id>/`) holds one row per `RelationArtifact` /
`ClaimArtifact` / `CausalEdgeFinding` that contributed to the card, with:

- the originating `chunk_id`, document, ingestion run ID
- the artifact's `confidence` and `evidence_text`
- the predicate or claim text as it appeared before canonicalization

This is what lets a compliance-trail query walk from a card back to source rows
(Step 6 of the slice document's walking-the-query example).

---

## What lands first

A useful minimum:

1. `routers.py` + `aggregators.py` for the relations path only.
2. A renderer for `link_type` and `object_type` cards.
3. A writer that diffs against `existing_cards`, bumps versions, archives old.
4. Defer causal, kinetic, dynamic emission to follow-up PRs once the relations
   path is exercising the diff/version flow on real ingestion output.

The foundry side is ready: `RelationPipeline`, `induce_schema`, and the
artifact types are stable and tested.
