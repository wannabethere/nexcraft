# Foundry Layer — Extractions and Models

The execution layer that takes seeded concepts from configuration and runs
the actual extraction work against tenant sources. The Foundry sits between
the configuration layer (what we're given) and the card generation layer
(what we produce). Its job is to anchor every extraction onto the seed,
extract the right things from each source type, and emit findings calibrated
to the source.

---

## 1. Purpose and Position

The pipeline now has a clear shape:

```
┌────────────────────────────────────────────────────────────────┐
│  CONFIGURATION LAYER                                            │
│  Platform / Pack / Tenant configs                               │
│  Seeded concepts, CDM references, causal priors, NER types     │
│  Schema mappings, source paths, mode declarations               │
└────────────────────────────────────────────────────────────────┘
                              │
                              ▼  passes config + seed knowledge
┌────────────────────────────────────────────────────────────────┐
│  FOUNDRY LAYER  (this document)                                 │
│                                                                  │
│  Loads seed knowledge into runtime state                        │
│  Runs source-mode-aware extraction operators                    │
│  Anchors every extraction to seed concepts and CDM              │
│  Emits typed findings with confidence calibration                │
│  Pluggable model selection per role and source mode             │
└────────────────────────────────────────────────────────────────┘
                              │
                              ▼  emits findings
┌────────────────────────────────────────────────────────────────┐
│  CARD GENERATION LAYER                                          │
│  Findings → candidate card edits → cards                        │
└────────────────────────────────────────────────────────────────┘
```

The Foundry is the layer where the actual work happens: documents get
chunked, NER runs, schemas get mapped, metadata gets extracted, sample data
gets correlated. Everything upstream of it is configuration; everything
downstream consumes its output.

What makes the Foundry "foundry" rather than just "extractors" is that
**it operates against a seeded foundation**. Every operator knows what
concepts exist, what CDM entities are canonical, what causal priors apply,
and what NER types matter — before it sees a single byte of tenant content.
This changes how operators work: less discovery from scratch, more
grounding into a known frame.

This document specifies:

- The seed loader that turns config into runtime state.
- The extraction operators organized by source family.
- The model registry — what models do what work, how they're selected.
- The schema mapping engine that aligns tenant tables to CDM entities.
- Confidence calibration mechanics.
- The output contract — what findings look like and how they flow downstream.
- How everything composes for a single pipeline run.

---

## 2. Architecture

```
┌────────────────────────────────────────────────────────────────────────┐
│  SEED LOADER                                                            │
│  Loads pack-shipped seed cards, CDM references, causal priors           │
│  Builds in-memory seed registry indexed for retrieval                   │
└────────────────────────────────────────────────────────────────────────┘
                              │
                              ▼
┌────────────────────────────────────────────────────────────────────────┐
│  EXTRACTION OPERATORS                                                   │
│                                                                          │
│  ┌──────────────────┐  ┌──────────────────┐  ┌──────────────────┐      │
│  │ Document         │  │ Schema           │  │ API Metadata     │      │
│  │ Extractors       │  │ Extractors       │  │ Extractors       │      │
│  │                  │  │                  │  │                  │      │
│  │ Chunkers         │  │ DDL parsers      │  │ Object metadata  │      │
│  │ NER pipelines    │  │ dbt manifests    │  │ Field metadata   │      │
│  │ Claim extractors │  │ Catalog imports  │  │ Validation rules │      │
│  │ Entity linkers   │  │ Schema mappers   │  │ Workflow rules   │      │
│  └──────────────────┘  └──────────────────┘  └──────────────────┘      │
│                                                                          │
│  ┌──────────────────┐  ┌──────────────────┐  ┌──────────────────┐      │
│  │ Data Extractors  │  │ Causal Extractors│  │ Outcome Extractors│      │
│  │                  │  │                  │  │                  │      │
│  │ Profilers        │  │ Discovery algos  │  │ Outcome streams  │      │
│  │ Correlators      │  │ Prior matchers   │  │ Label joiners    │      │
│  │ FK validators    │  │ Refutation tests │  │ Window aggregators│      │
│  └──────────────────┘  └──────────────────┘  └──────────────────┘      │
└────────────────────────────────────────────────────────────────────────┘
                              │
                              ▼
┌────────────────────────────────────────────────────────────────────────┐
│  MODEL REGISTRY                                                         │
│  Pluggable models: NER, embeddings, LLMs, causal, statistical            │
│  Routing by role and source mode                                         │
│  GPU/CPU/API budget management                                           │
└────────────────────────────────────────────────────────────────────────┘
                              │
                              ▼
┌────────────────────────────────────────────────────────────────────────┐
│  FINDINGS BUS                                                           │
│  Typed, calibrated, provenance-tracked artifacts                        │
│  Feeds card generation downstream                                        │
└────────────────────────────────────────────────────────────────────────┘
```

Every operator has access to: the seed registry, the model registry, the
findings bus, and the execution context (Spark / standalone / Ray, from the
extraction design).

---

## 3. Seed Loader

The seed loader is the first thing that runs in any pipeline session. It
turns the static configuration files into runtime state that every operator
can query.

### 3.1 What it loads

From the pack and tenant configs:

- **Seed concept cards** — pre-built `concept` cards shipped with the pack.
- **Seed object_type / link_type / property_type cards** — CDM-derived
  scaffold cards.
- **CDM references** — canonical entity definitions (NIST CSF controls,
  MITRE ATT&CK techniques, OMOP tables, MedDRA terms, etc.).
- **Causal priors** — pack-shipped `causal_edge` cards with
  `weight.source: literature` or `pack_default`.
- **NER type set** — domain entity types beyond platform defaults.
- **Rule templates** — derivation and validation rule patterns to instantiate
  during extraction.
- **Ontology imports** — large external ontologies loaded as concept hierarchies.
- **Tenant overrides** — any tenant-customized cards from `pack_overrides_dir`.

### 3.2 What it builds

The loader produces a single in-memory `SeedRegistry` object with multiple
indices:

```python
class SeedRegistry:
    # Concept lookup by canonical name and aliases
    concepts_by_name: Dict[str, ConceptCard]
    concepts_by_id: Dict[str, ConceptCard]
    
    # CDM entity inventory
    cdm_entities: Dict[str, CdmEntity]   # cdm.identity.user, cdm.training.assignment, etc.
    cdm_relationships: Dict[str, CdmRelationship]
    
    # Causal scaffold
    causal_priors: Dict[str, CausalEdgeCard]
    causal_node_seeds: Dict[str, CausalNodeCard]
    
    # NER type set (for GLiNER initialization)
    ner_types: List[NerType]
    causal_marker_lexicon: Set[str]
    
    # Rule template inventory
    derivation_rule_templates: Dict[str, DerivationRuleTemplate]
    validation_rule_templates: Dict[str, ValidationRuleTemplate]
    
    # Ontology hierarchies
    ontologies: Dict[str, OntologyHierarchy]   # CWE, ATT&CK, MedDRA, etc.
    
    # Embedding index for semantic seed lookup
    seed_embedding_index: QdrantCollection
```

Operators query these structures rather than re-deriving knowledge from
scratch. An entity linker checks `concepts_by_name` first; a NER pipeline
initializes its types from `ner_types`; a claim extractor passes
`causal_marker_lexicon` to its grounding prompt.

### 3.3 The seed embedding index

Concepts and CDM entities all get embedded at load time and indexed in a
local Qdrant collection scoped to this run. This is the **seed embedding
index** — it's separate from the tenant card embedding index because seeds
are stable, fast to load, and queried at every extraction step.

The seed index is what makes "anchor to seed" cheap. When NER finds a span
"phishing risk awareness" in a document, the entity linker queries the seed
index first — if the span maps to the pack's `PhishingRisk` seed concept
above the similarity threshold, it links there immediately. Only spans that
fail to link to the seed get embedded against the tenant card index, which
is larger and more expensive.

### 3.4 Validation at load

Before the registry is considered loaded, it passes validation:

- Every CDM reference must resolve to actual entity definitions.
- Every causal prior must reference seed concepts that exist.
- Every NER type must be unique within the type set.
- Tenant overrides must reference real pack cards.
- Rule templates must compile (their parameter schemas validate).

A failure here is a configuration error, not a runtime error. The pipeline
refuses to start.

### 3.5 Hot-reload semantics

The seed registry is mostly static within a run. Pack upgrades reload it;
tenant config changes reload the affected sections. For long-running
pipelines, the loader supports incremental reload of specific seed
categories without restarting the operator pool.

---

## 4. Extraction Operators

Six families of extraction operators, each handling a different class of
input. Every operator is **seed-aware** — it has access to the seed registry
and uses it to ground its output.

### 4.1 Document Extractors

Operators that turn unstructured text into typed findings.

#### Document Chunkers

Per the ingestion plan §11.1: structure-aware splitting per document type.
Markdown header chunker, PDF structural chunker, slide deck chunker, code
AST chunker, recursive token chunker as fallback. Each emits chunks with
heading paths and adjacency pointers.

The Foundry adds one thing the original spec didn't have: **seed-aware
chunk classification**. After chunking, a lightweight classifier labels each
chunk with the seed concepts most likely to be relevant. The classifier is
a fast embedding similarity match against the seed embedding index. Chunks
with no seed-concept matches above threshold get flagged for new-concept
candidate review.

This serves two purposes: it routes downstream operators to the right
chunks (a claim extractor for governance only runs on chunks tagged with
governance concepts), and it surfaces gaps where the document covers
content the seed doesn't anticipate.

#### NER Pipelines

The hybrid NER from the ingestion plan §5.4: spaCy + GLiNER + rule-based
causal markers. The Foundry adjustment: **GLiNER is initialized with the
seed registry's NER types**, not a hardcoded set. Pack-shipped types
(`policy_clause`, `control_id`, `meddra_term`, etc.) flow through directly.

Per chunk, the pipeline produces `EntitySpanArtifact`:

```
{
  "chunk_id": "doc_47/section_3/chunk_12",
  "spans": [
    {
      "text": "phishing simulation training",
      "type": "concept",
      "model": "gliner",
      "char_start": 142,
      "char_end": 169,
      "confidence": 0.91,
      "seed_anchor": "PhishingTrainingProgram"  // if entity linker resolved it
    },
    {
      "text": "reduces",
      "type": "causal_marker",
      "model": "rule_based",
      "char_start": 170,
      "char_end": 177,
      "confidence": 1.0
    },
    {
      "text": "successful phishing attempts",
      "type": "concept",
      "model": "gliner",
      "char_start": 178,
      "char_end": 207,
      "confidence": 0.88,
      "seed_anchor": "PhishingIncidentRate"
    },
    {
      "text": "by ~40%",
      "type": "quantitative_claim",
      "model": "gliner",
      "char_start": 208,
      "char_end": 215,
      "confidence": 0.95
    }
  ]
}
```

The combination of three concept spans plus a causal marker plus a
quantitative claim in proximity is what tells the downstream claim
extractor "this looks like a hypothesizable causal_edge with prior 0.4."

#### Claim Extractors

The LLM-based extractors from the ingestion plan §5.5. The Foundry adjustment:
**the prompt is parameterized by seed knowledge**.

Instead of a single generic claim extractor, the Foundry runs four claim-
type-specific extractors, each with a prompt that includes the relevant
seed knowledge:

- **DefinitionExtractor** — prompt includes the seed property and concept
  inventory: "the following properties exist: [list]; extract definitions
  that refine or describe them."
- **RuleExtractor** — prompt includes the seed rule templates: "the
  following rule templates exist: [list]; extract rules from this chunk
  that match these patterns or extend them."
- **CausalClaimExtractor** — prompt includes the seed causal priors and
  the causal marker lexicon: "the following causal priors are known:
  [list]; extract causal claims from this chunk, marking whether they
  support, refute, or extend known priors."
- **GovernanceExtractor** — prompt includes the seed roles, permissions,
  and markings: "the following access patterns exist: [list]; extract
  governance claims from this chunk."

Each extractor produces typed `ClaimArtifact` with provenance and confidence.

The seed-grounding does three things:

1. Reduces hallucination — the LLM has explicit named anchors to use.
2. Improves entity linking downstream — claims arrive pre-linked to seed
   IDs.
3. Surfaces deltas — claims that *extend* known seed knowledge are flagged
   distinctly from claims that simply restate it, which lets card
   generation prioritize updates over restatements.

#### Entity Linkers

Per the ingestion plan §5.6, with the Foundry-specific change: **the seed
embedding index is queried first**, before the tenant card index. A span
that resolves to a seed concept gets linked there with a "seed_anchor"
flag. Only spans that fail seed lookup go through tenant card lookup, then
through the new-entity candidate flow.

This is what makes the pipeline efficient for metadata-rich packs. An
eClinical pack ships with thousands of seed concepts (MedDRA, SNOMED,
RxNorm, OMOP); the vast majority of clinical document content links there
without ever touching the tenant index.

### 4.2 Schema Extractors

Operators that turn schema declarations into typed findings, anchored to
the CDM scaffold.

#### Schema Profiler (no data required)

The Foundry's schema profiler reads DDL, dbt manifests, or catalog exports
and emits `SchemaArtifact`:

```
{
  "schema_id": "csod",
  "tables": [
    {
      "name": "employee",
      "columns": [
        { "name": "employee_id", "type": "varchar", "nullable": false, "is_pk": true },
        { "name": "department_id", "type": "int", "nullable": true, "fk_to": "department.department_id" },
        ...
      ],
      "comment": "Employee master record",
      "row_count_estimate": null,
      "metadata_only": true
    }
  ],
  "foreign_keys": [...],
  "indices": [...]
}
```

This operator runs for every source mode — schemas are always extractable
when present. Profiling statistics get filled in later by the Column
Profiler if data is accessible; if not, the schema artifact stands on its
own.

#### Schema Mapper (CDM alignment)

The first operator that uses seed knowledge structurally. It takes the
schema artifact and the tenant config's `schema_mapping` declarations and
produces a `MappedSchemaArtifact` where every table is bound to a CDM
entity.

```
{
  "mappings": [
    {
      "tenant_table": "csod.employee",
      "cdm_entity": "cdm.identity.user",
      "source": "tenant_declared",
      "confidence": "high"
    },
    {
      "tenant_table": "salesforce.Contact",
      "cdm_entity": "cdm.identity.external_user",
      "source": "tenant_declared",
      "confidence": "high"
    },
    {
      "tenant_table": "csod.unknown_custom_table",
      "cdm_entity": null,
      "source": "unmapped",
      "confidence": "n/a",
      "candidates": [
        { "cdm_entity": "cdm.training.assignment", "similarity": 0.62 },
        { "cdm_entity": "cdm.identity.user_attribute", "similarity": 0.41 }
      ],
      "needs_hitl": true
    }
  ]
}
```

The mapper handles three cases:

1. **Tenant-declared mapping** — the tenant config specifies the binding
   directly. The mapper validates that the named CDM entity exists.
2. **LLM-inferred mapping** — for tables not in the declaration, the
   mapper builds a description from the table's columns and embeds it,
   then queries the CDM entity inventory for top-k candidates. The LLM
   judges whether any candidate is a confident match (above 0.85
   embedding similarity *and* a positive LLM verdict).
3. **Unmapped** — tables that don't match any CDM entity flow to a HITL
   queue. The pipeline doesn't infer mappings under uncertainty; better
   to leave a table unmapped than to mis-map it.

The mapped schema flows downstream: subsequent operators traversing
relationships use CDM-scoped queries when possible. "Find every entity
of type cdm.identity.user across all sources" becomes a meaningful
query because the mapper has bound each tenant table to its CDM identity.

#### Property Type Extractor

For each column in the mapped schema, this operator produces a
`PropertyTypeArtifact`. Type information comes from the schema; semantic
information comes from:

- The seed registry's property catalog (does a seed property already exist
  for this CDM entity + column-name pattern?).
- LLM enrichment over the column comment, if present.
- The pack's rule templates (does a derivation rule template apply to
  this column?).

Properties carry the confidence tier reflecting how they were derived.
Schema-only with no comment: medium. Schema + LLM enrichment from comment:
medium-high. Schema + match to seed property: high.

#### Foreign Key Extractor

Reads the schema artifact's declared FKs and emits `LinkTypeArtifact`s with
`derivation: structural` and `confidence: high`. These are the easiest
findings to produce — they're declarations.

For schemas without explicit FKs (common in older databases or CSV
exports), an LLM-based FK inferer matches column-name patterns against
the seed CDM relationship inventory. Inferred FKs get
`derivation: structural`, `confidence: medium`, and route to HITL for
confirmation.

### 4.3 API Metadata Extractors

The operators that handle Mode B and Mode C from the configuration doc.
These are the highest-leverage operators in metadata-rich-but-data-poor
deployments.

#### API Metadata Adapter (per system)

Per the configuration doc §5.3, every API source has an adapter
implementing:

```python
class ApiMetadataAdapter:
    def fetch_metadata(self) -> ApiMetadataArtifact: ...
    def fetch_sample(self, object_name: str, limit: int) -> Optional[DataArtifact]: ...
    def supports_data_access(self) -> bool: ...
```

Adapters ship for common APIs. The Foundry includes adapters for: Salesforce,
Workday, ServiceNow, Cornerstone OnDemand, Veeva Vault, Jira, NetSuite,
Microsoft Graph (for SharePoint, Teams). Each is ~500-1500 lines of typed
metadata pulling code.

The output `ApiMetadataArtifact` is normalized across systems:

```
{
  "system": "salesforce",
  "objects": [
    {
      "name": "Contact",
      "label": "Contact",
      "description": "...",
      "is_custom": false,
      "fields": [
        {
          "name": "Email",
          "type": "email",
          "nullable": true,
          "label": "Email",
          "description": "...",
          "max_length": 80
        },
        {
          "name": "AccountId",
          "type": "lookup",
          "lookup_to": "Account",
          "nullable": false,
          "label": "Account",
          "description": "..."
        },
        {
          "name": "Lead_Source__c",
          "type": "picklist",
          "picklist_values": [
            { "value": "Web", "label": "Web", "is_default": false },
            { "value": "Referral", "label": "Referral", "is_default": false },
            ...
          ]
        }
      ],
      "validation_rules": [
        {
          "name": "Email_Required_For_Active",
          "expression": "AND(IsActive__c = TRUE, ISBLANK(Email))",
          "error_message": "Email required for active contacts"
        }
      ],
      "workflow_rules": [
        {
          "name": "Set_Customer_Status",
          "trigger": "After insert/update",
          "criteria": "Stage__c = 'Closed Won'",
          "actions": [
            { "type": "field_update", "target_field": "Account.Status__c", "new_value": "Customer" }
          ]
        }
      ]
    }
  ],
  "profiles": [...],
  "permission_sets": [...],
  "sharing_rules": [...]
}
```

The normalization across systems is what lets downstream operators work
generically. A workflow rule extractor doesn't need to know whether it's
processing Salesforce, Workday, or Veeva — the artifact shape is the same.

#### Object Type Extractor (from API metadata)

Produces `ObjectTypeArtifact`s from the metadata. For each `object` in the
artifact:

- Card body draft = label + description + a generated overview from the
  field set.
- Header refs = mapped CDM entity (looked up via Schema Mapper) + linked
  related objects (from lookup/master-detail relationships).
- Confidence tier = medium (metadata only, no data validation).

#### Property Type Extractor (from API metadata)

Produces `PropertyTypeArtifact`s. Picklist fields produce a property *and*
companion concept cards for each picklist value (since picklist values are
domain concepts). Reference fields contribute to link extraction, not
property extraction.

#### Validation Rule Extractor

Produces `ValidationRuleArtifact`s from declared validation rules. The
expression is captured verbatim; the card body is generated by an LLM that
explains what the expression means in prose.

#### Workflow Rule Extractor

Produces `CausalRuleArtifact`s and accompanying hypothesized
`CausalEdgeArtifact`s. A workflow rule with criteria "Stage = Closed Won"
and action "set Account.Status = Customer" becomes:

- A `CausalRule` card with the trigger condition and action declared.
- A `CausalEdge` from `Stage` to `Account.Status` with
  `weight.source: declared_in_system` and `weight.value: 1.0` (deterministic).
- Identifiability: trivially identifiable (it's declared, not inferred).

Workflow rules are the strongest "free" causal information in any API
system. They're declarative and deterministic; they cost nothing to learn.

#### Permission/Role/Marking Extractor

Produces `RoleArtifact`s, `PermissionArtifact`s, and `MarkingArtifact`s
from profile and permission set metadata. Field-level security on
PII-relevant fields (emails, names, IDs) generates marking propagation
rules.

### 4.4 Data Extractors (Mode A and Mode B)

Operators that require sample data. The Foundry implements them as the
**four-tier statistical correlation pipeline** specified in extraction
design §3.3, with each tier as its own operator family. Each tier has a
clean degradation strategy when data is unavailable or limited.

#### Column Profiler

Per the extraction design §3.2: type, null rate, distinct count, top-k,
percentiles, histograms. With Mode B sample-bounded execution:

- Null rate from a 100K sample is reliable; flagged as "approximate."
- Distinct count from a sample uses HyperLogLog; reliable for cardinality
  estimates above 100, less so below.
- Top-k from a sample is reliable for high-frequency values, less for tail.
- Percentiles use t-digest; reasonably accurate at the sample size.

Output `ColumnProfileArtifact` carries the sample size and the
"approximate" flag. Downstream operators that consume profiles read these
flags and adjust confidence accordingly.

In Mode C (no data), the operator emits a `StatsUnavailableArtifact`
recording the column was profileable in principle but no data was
accessible. Downstream consumers see this and skip distribution-dependent
work for that column.

#### Tier 1 Pre-filter (Candidate Pair Generator)

Pure metadata reasoning. Runs in every source mode. Drops pair candidates
that are structurally implausible *before* any statistical work begins,
reducing 12M-pair candidate spaces to 100K-500K typically.

Filters applied in order:

1. **Schema-level pruning** — drop pairs across CDM entities that don't
   relate per the seed registry's CDM relationship inventory.
2. **Type-level pruning** — drop type-incompatible pairs (free-text vs
   numeric, ID vs ID).
3. **Cardinality-level pruning** — drop columns that are >99% same value,
   >95% null, >99% unique. Read from Column Profiler outputs in Mode A/B;
   from API metadata cardinality flags in Mode C.
4. **Embedding-level pruning** — embed each column's name + comment +
   sample top-k values. Pairs below the 70th percentile of similarity
   scores get dropped.
5. **Seed-prior reordering** — pairs participating in pack causal priors
   get prioritized in the output ordering. (Doesn't drop; influences
   processing order downstream.)

Output: `CandidatePairArtifact` listing pairs to test, ordered by priority.
Runs in any source mode; in Mode C the embedding-level filter still works
because column names and comments don't require data access.

#### Tier 2 Vectorized Statistical Screening

Cheap, vectorized statistical tests across all surviving candidate pairs
from Tier 1. Pure math layer, **LLM-free**.

Per pair-type:
- Numeric ↔ Numeric: Spearman (default), Pearson, distance correlation
- Categorical ↔ Categorical: Cramér's V, Theil's U
- Mixed: phik (φK), correlation ratio η
- Catch-all: mutual information

Multiple-testing correction: Benjamini-Hochberg FDR within scope (within
Spearman tests separately from Cramér's V tests, etc.). Effect-size
threshold scaled to sample size: `max(0.1, 2/sqrt(n))`.

Stratification by seed-known axes (e.g., `Role` for training-related
pairs). Simpson-paradox detection when stratified and unstratified results
diverge meaningfully — surfaced as a separate finding.

Output: `CorrelationFindingArtifact` per surviving pair (1K-10K typical).
Each includes method, statistic, FDR-corrected p-value, n, effect size
flag, and stratification result if applicable.

Mode A: full execution. Mode B: sample-bounded with sample size in
findings. Mode C: skip entirely; emit `StatsUnavailableArtifact` covering
the candidate pairs that couldn't be tested.

#### Tier 3 Targeted Expensive Analysis

For pairs that survived Tier 2, run analyses suitable for causal reasoning.
Pure math layer for the analyses themselves; bounded LLM context only for
proposing candidate confounders on non-seed pairs.

Operations:

- **Bootstrap CIs** for stability of effect estimates.
- **Conditional independence tests** with seed-declared confounders first;
  for non-seed pairs, an LLM proposes candidate confounders (output: list
  of column names) and the conditional independence test validates each.
  The math has the final say — a confounder that doesn't change the
  partial correlation meaningfully is rejected.
- **Partial correlation** with confounders to quantify residual effect.
- **Granger causality** and **cross-correlation with lags** for
  time-series pairs.
- **Transfer entropy** for non-linear time-series causality.
- **Refutation tests** (placebo treatment, random common cause, data
  subset stability) on high-stakes correlations.

Output: `ValidatedCorrelationArtifact` per pair (100-500 typical), with
all Tier 2 fields plus bootstrap CI, conditional independence results,
refutation outcomes, and any LLM-proposed confounder candidates that the
math validated.

Mode A: full execution. Mode B: sample-bounded with conservative test
selection (some methods need more data than others). Mode C: skip.

#### Tier 4 Causal Discovery Suite

Per the extraction design §3.3 (Tier 4): PC, FGES, GES, LiNGAM, NOTEARS,
PCMCI. Run on the **variables that survived as participants in Tier 3
findings** — typically 50-200 variables, not the full schema. Without
this restriction, discovery on 5000+ columns is computationally infeasible.

Multiple algorithms run in parallel; the `CausalConsensus` operator
intersects outputs. High-agreement edges (3+ algorithms agree on direction)
become hypothesized `CausalEdgeArtifact`s with elevated initial confidence.
Disagreement edges become hypothesized cards with lowered confidence and
a prose note about algorithmic disagreement.

**Discovery output is direct edges only** — no transitive closure, no
multi-hop paths. Multi-hop reasoning is the responsibility of the query
layer, computed at depth-3 default from this direct-edge corpus.

Mode A: full execution. Mode B: tolerant algorithms only (PC, LiNGAM);
findings flagged `weight.source: hypothesized_from_sample`. Mode C: skip
discovery entirely; rely on Causal Prior Matcher for causal content
(see §4.5).

#### FK Validator

For declared FKs, computes the empirical referential integrity rate (do all
foreign keys actually exist in the target table?). Mode A: full computation.
Mode B: sample-based estimate. Mode C: skipped, FKs assumed valid as
declared.

A low FK validity rate (say, < 95%) is itself a finding worth surfacing —
it indicates data quality issues that should be reflected in card
confidence.

### 4.5 Causal Extractors

The operators that produce causal findings. The Foundry's causal
extractors are seed-aware in two ways: they match against pack causal
priors first, and they emit **direct causal edges only** (no transitive
closure, no stored multi-hop paths). Multi-hop reasoning is the
responsibility of the query layer, which computes paths on demand from
this direct-edge corpus, bounded at depth 3 by default.

#### Causal Prior Matcher

Runs first among causal operators. For each pack-shipped causal prior,
checks whether the participating concepts exist in the tenant's ontology
(either from seeds, from API metadata, or from documents). If both source
and target concepts exist, the prior is "active" — it becomes a
hypothesized `CausalEdgeArtifact` with `weight.source: literature` or
`pack_default`.

This is the operator that gives metadata-only deployments their causal
foundation. Without any data, the pipeline can already populate the causal
graph with literature-backed edges keyed to seed concepts. In Mode C, this
is the *only* causal operator that produces output, and it produces enough
to make depth-3 causal queries meaningful.

#### Causal Discovery Reconciler

Reads Tier 4 discovery output (when data is available) and reconciles each
discovered edge against existing prior edges:

1. **Discovery confirms a prior** — the prior's confidence increases; the
   weight may refit if the data supports it.
2. **Discovery contradicts a prior** — flag for HITL. The prior says
   "phishing training reduces phishing rate"; the data says effect is
   weaker or reversed. This is interesting and important; it doesn't
   silently overwrite either.
3. **Discovery finds a new edge** — emit as a fresh hypothesized
   `CausalEdgeArtifact`. Subject to validation, it joins the graph.

The reconciler outputs only direct edges. Transitive paths between
discovered edges are not materialized; they're computed at query time.

#### Refutation Tester

For high-stakes causal edges (those above an importance threshold or
flagged as governance-critical), runs DoWhy-style refutation tests on
sample data when available:

- **Placebo treatment** — replace the actual treatment variable with a
  random one; effect should disappear.
- **Random common cause** — add a random confounder; effect should
  persist if true.
- **Data subset stability** — refit on subsets; effect should be stable.

Edges that fail refutation get demoted to hypothesized regardless of how
they were learned. Refutation results live in the edge's evidence history.

#### Causal Path Pre-Computer (post-extraction, optional)

Not strictly an extractor but worth listing here because it lives in the
Foundry. After the direct edges are written, this operator pre-computes
depth-3 subgraphs for the top-N target nodes (per query log analysis or
seed-marked importance) and caches them in Redis. This is the cache
warming step for the graph maintainer's hot-path queries.

The operator runs nightly; it's not in the critical path of card
generation. If it's skipped, depth-3 queries still work — they just
traverse from direct edges live, which is acceptable but slower.

### 4.6 Outcome Extractors

Operators that bring labeled outcome data into the pipeline. Per the
extraction design §3.10, three time horizons (fast / medium / slow). The
Foundry-specific concern is that outcomes also need seed alignment —
"this outcome event belongs to the OverdueAssignment concept" or "this
adverse event belongs to the SeriousAdverseEvent concept" — so the Weight
Learner downstream knows which causal edges to update.

The outcome extractor emits `OutcomeArtifact`s with seed-concept tags:

```
{
  "outcome_batch_id": "csod_overdue_2026-04",
  "horizon": "fast",
  "events": [
    {
      "event_id": "...",
      "timestamp": "...",
      "outcome_concept": "OverdueAssignment",
      "subject_id": "ta_991",
      "outcome_value": true,
      "feature_snapshot": {...}
    }
  ]
}
```

The seed concept tag is what binds outcomes to causal edges. Without it,
the Weight Learner can't decide which edges this outcome is evidence for.

---

## 5. Model Registry

The Foundry's model layer. Models are pluggable, role-routed, and
selectable per source mode.

### 5.1 Roles, not models

Operators don't request specific models; they request **roles**. A role is
a named capability with quality and cost characteristics:

| Role                      | Used by                                       |
| ------------------------- | --------------------------------------------- |
| `embedder_default`        | Card and concept embedding                    |
| `embedder_lite`           | Auxiliary indices, hot-path retrieval         |
| `ner_generic`             | spaCy-equivalent for generic types            |
| `ner_domain`              | GLiNER-equivalent for pack types              |
| `ner_medical`             | Clinical NER (eClinical only)                 |
| `claim_extractor_strong`  | Causal claims, identifiability prose           |
| `claim_extractor_routine` | Definitions, rules, governance                |
| `summarizer`              | Card summaries, compression                   |
| `judge`                   | LLM-judge in eval framework                   |
| `mapper_judge`            | Schema mapping verdict                        |
| `code_inferencer`         | Validation rule explanation, formula prose    |
| `causal_discovery_pc`     | PC algorithm                                  |
| `causal_discovery_fges`   | FGES algorithm                                |
| `causal_effect_estimator` | DoWhy-equivalent                              |
| `weight_learner_logistic` | Logistic regression for weights               |
| `weight_learner_gbm`      | Gradient-boosted trees for weights            |

Operators write `context.run_role("ner_domain", chunks)` rather than
`context.run("gliner", chunks)`. The model registry handles routing.

### 5.2 Routing rules

The platform config declares a default model for each role. The pack can
override per-role. The tenant can override per-role. The operator can
provide a hint based on input characteristics ("this is a high-stakes card,
prefer strong"). Resolution order:

1. Operator hint
2. Tenant override
3. Pack override
4. Platform default

The registry resolves at call time. The same operator can use
Anthropic Claude Opus for one call and Claude Haiku for another, depending
on the input characteristics.

### 5.3 Model deployment

Models come from four deployment modes:

- **API (cloud)** — Anthropic API, OpenAI API, Cohere API. Default for LLMs
  and embedders.
- **Self-hosted (Ray Serve)** — vLLM, TGI, sentence-transformers behind
  Ray Serve. Used for cost-sensitive deployments and for self-hosted
  customer requirements.
- **In-process (Python)** — spaCy, GLiNER, scikit-learn, scipy,
  causal-learn. The fastest option but needs CPU/GPU on the executor.
- **External service** — customer-provided model endpoints (compliance-
  required isolation).

The registry abstracts these uniformly. Operators don't know how a model
is served; they just call its role.

### 5.4 Resource budgets

Every model call carries cost. The registry enforces budgets per pipeline
run:

- Per-role token budget (LLM roles).
- Per-role rate limit (API roles).
- Per-run total cost cap.
- Per-tenant daily cost cap.

Budget exhaustion is handled by the operator's degradation strategy:
typically falling back to a cheaper role, or skipping with a recorded
"budget exhausted" finding.

### 5.5 Caching

Every model call is cached by content hash. The cache key is
`(role, model_resolved, input_hash)`. The registry handles cache lookups
and inserts transparently. Operators don't manage caches; they just call
roles.

This is what makes incremental pipeline runs cheap. A document chunk that
hasn't changed since last run hits cache for every role it triggered:
NER cache hit, claim extraction cache hit, embedding cache hit. The
operator runs to completion in microseconds.

---

## 6. Source Mode Awareness

Every operator knows which source modes it supports and how it degrades.
Per the configuration doc §6, the matrix is precise; the Foundry implements
it.

### 6.1 The contract

Every operator declares:

```python
class FoundryOperator:
    name: str
    
    # Which source modes this operator can run in
    supported_modes: Set[SourceMode]
    
    # What inputs it requires (and what's optional)
    required_inputs: List[ArtifactType]
    optional_inputs: List[ArtifactType]
    
    # What it produces in each mode
    outputs_by_mode: Dict[SourceMode, List[ArtifactType]]
    
    # What it degrades to when an optional input is missing
    degradation_strategy: DegradationStrategy
```

The pipeline planner inspects this contract. If a tenant has Mode C
sources, the planner skips operators whose `supported_modes` doesn't include
Mode C, marking them "skipped (mode unsupported)" rather than failing.

### 6.2 Degradation strategies

Three degradation patterns, declared per operator:

1. **Skip cleanly** — the operator emits an "unavailable" artifact and
   completes. Downstream operators see the unavailable artifact and adjust.
2. **Reduce confidence** — the operator runs with reduced inputs but
   completes; output carries lowered confidence tier.
3. **Substitute** — the operator uses a substitute input (sample instead
   of full data, metadata instead of stats); output is calibrated
   accordingly.

A correlator on Mode C: skip cleanly. A correlator on Mode B with a small
sample: reduce confidence. A property type extractor without column data:
substitute (use schema + comment instead of sampled distribution).

### 6.3 Provenance through degradation

Every artifact carries enough provenance to reconstruct what was and
wasn't done:

```
{
  "artifact_type": "PropertyTypeFinding",
  "subject": "csod.training_assignment.progress_percent",
  "confidence_tier": "medium",
  "derivation_path": [
    { "operator": "schema_extractor", "input_mode": "ddl", "confidence": "high" },
    { "operator": "column_profiler", "input_mode": "sample_bounded", "sample_size": 10000, "confidence": "medium" },
    { "operator": "comment_enricher", "input_mode": "llm", "confidence": "medium" }
  ],
  "missing_inputs": ["full_table_scan_stats", "outcome_distribution"]
}
```

The derivation path is what the eval framework's grounding checks consume.
"This card claims X with confidence medium because it had access to schema,
sample, and LLM enrichment, but not full stats or outcomes" — that's a
defensible position. "This card claims X with confidence high" with no
provenance — that's a problem.

---

## 7. The Findings Bus

The output channel for everything Foundry produces. Every operator writes
to the findings bus; downstream card generation reads from it.

### 7.1 Finding types

The Foundry produces findings at multiple levels of granularity:

- **Atomic findings** — one fact at a time. "Column X has null rate 0.34 in
  this 10K sample." "Workflow rule Y triggers field update Z."
- **Compound findings** — multi-element observations. "Schema mapping for
  csod.employee = cdm.identity.user with confidence high, derivation path
  [tenant_declared]."
- **Aggregate findings** — roll-ups. "47 hypothesized causal edges from
  literature priors are now active for this tenant given the available
  concepts."

Each type has a typed schema. Card generation operators expect specific
finding types as inputs.

### 7.2 Provenance and lineage

Every finding carries:

- The operator that produced it (with version).
- The inputs it consumed (with hashes).
- The model role it called (with cost).
- The timestamp.
- The seed elements referenced.
- The confidence tier.
- Any HITL flags.

This is what makes the eval framework's source-grounding check possible:
every finding can be re-traced to its origins.

### 7.3 Storage

Findings are written to the artifact store from §10.1 of the extraction
design. Spark backend writes Delta tables; standalone writes Postgres +
Parquet; Ray writes to the object store with Parquet persistence.

Findings are queryable. The card generation layer reads them via typed
queries: "give me all CausalClaimFindings produced in the current run with
confidence_tier in [high, medium]." This decouples Foundry from card
generation cleanly.

### 7.4 Lifecycle

Findings have a defined lifecycle:

1. **Emitted** by a Foundry operator into the bus.
2. **Consumed** by one or more card generation operators.
3. **Retained** for the run's lifetime.
4. **Archived** after the run completes (for forensics and incremental
   runs).
5. **Pruned** after the retention window (typically 90 days, configurable
   per finding type).

---

## 8. End-to-End Walkthrough

A concrete walkthrough of one Foundry run, mixing source modes.

**Tenant**: `acme_corp`, active pack `lms_security_v2.3`.

**Sources**:
- CSOD in Snowflake (Mode A, full data + outcomes)
- Salesforce CRM (Mode B, sample data permitted, 1000 rows per object)
- Cornerstone Compliance (Mode C, metadata only)
- 24 policy PDFs (Mode D)

### 8.1 Seed loading

Pipeline starts. SeedLoader runs:

```
[02:31:00] Loading platform config
[02:31:01] Loading pack lms_security_v2.3:
  - 142 seed concepts (PhishingRisk, ComplianceGap, etc.)
  - 89 seed object types from CDM (cdm.identity.user, etc.)
  - 234 hypothesized causal_edges from literature
  - 47 NER types (policy_clause, control_id, etc.)
  - 18 rule templates
  - Imports: NIST CSF 2.0, MITRE ATT&CK v15, CWE 4.13
[02:31:18] Loading tenant config and overrides
[02:31:20] Building seed embedding index (8312 entries embedded)
[02:31:42] Validating seed registry
[02:31:43] SeedRegistry ready
```

22 seconds of setup. The seed is in memory and indexed.

### 8.2 Extraction begins, parallelizing across sources

Four extraction streams run in parallel.

**Stream 1: CSOD warehouse (Mode A)**
```
[02:31:43] Schema profiler reading dbt manifest
[02:31:46] Schema mapper aligning 47 tables to CDM
  - 43 tenant-declared mappings validated
  - 3 LLM-inferred mappings (high confidence)
  - 1 unmapped table → HITL queue
[02:32:01] Column profiler running on 312 columns (full data)
[02:35:14] Correlator running on profiled columns
  - 8412 pairs tested
  - 312 significant correlations emitted
[02:42:22] Causal discovery (PC + FGES + LiNGAM in parallel)
  - 187 candidate edges from PC
  - 142 from FGES
  - 96 from LiNGAM
  - Consensus: 78 high-agreement edges
[02:51:08] Causal prior matcher
  - 234 priors checked
  - 47 priors active (concepts present)
  - 12 confirmed by discovery
  - 2 contradicted (HITL)
  - 33 unconfirmed but kept
[02:51:33] Outcome extractor pulling overdue events
  - 14820 events seed-tagged
[02:51:48] Refutation tester on 14 high-stakes edges
  - 13 pass
  - 1 fails placebo test → demoted
```

**Stream 2: Salesforce (Mode B)**
```
[02:31:43] API metadata adapter pulling sObject definitions
  - 89 objects, 1240 fields, 47 validation rules, 18 workflow rules
[02:31:58] Object type extractor → 89 ObjectTypeArtifacts
[02:32:04] Property type extractor → 1240 PropertyTypeArtifacts
[02:32:09] FK extractor from declared lookups → 312 LinkTypeArtifacts
[02:32:11] Validation rule extractor → 47 ValidationRuleArtifacts
[02:32:14] Workflow rule extractor → 18 CausalRuleArtifacts + 18 hypothesized CausalEdgeArtifacts
[02:32:16] Permission extractor → 23 RoleArtifacts, 89 PermissionArtifacts
[02:32:20] Sample data fetch (1000 rows × 89 objects, rate-limited)
[02:38:42] Column profiler on samples → 1240 ColumnProfileArtifacts (sample-bounded)
[02:42:01] Correlator on samples → 47 correlation findings
```

**Stream 3: Cornerstone (Mode C)**
```
[02:31:43] API metadata adapter
  - 23 objects, 187 fields, 8 formula fields, 12 validation rules
[02:31:52] Object type extractor → 23 ObjectTypeArtifacts (medium confidence)
[02:31:58] Property type extractor → 187 PropertyTypeArtifacts (medium, no distributions)
[02:32:01] FK extractor → 56 LinkTypeArtifacts
[02:32:04] Formula field → derivation_rule extractor → 8 DerivationRuleArtifacts
[02:32:07] Validation rule extractor → 12 ValidationRuleArtifacts
[02:32:08] Operator skips: column profiler, correlator, causal discovery
  - reason: Mode C, no data accessible
  - "stats unavailable" markers emitted
```

**Stream 4: Policy PDFs (Mode D)**
```
[02:31:43] PDF chunker on 24 documents
  - 312 chunks emitted
  - heading paths preserved
[02:32:18] Seed-aware chunk classifier
  - 247 chunks tagged with seed concepts
  - 65 chunks flagged as outside seed (new-concept candidates)
[02:32:42] NER pipeline (spaCy + GLiNER + causal markers)
  - 8412 spans across all chunks
[02:33:17] Entity linker
  - 7321 spans linked to seed concepts
  - 891 spans linked to existing tenant cards
  - 200 spans queued as new entity candidates
[02:34:08] Claim extractor (parallel: definitions, rules, causal, governance)
  - 47 definitions
  - 89 rules
  - 142 causal claims (matched against 47 active priors)
  - 23 governance claims
```

### 8.3 Convergence

All four streams complete by 02:52. The findings bus now contains:

- ~15K typed findings across the four streams
- Confidence tiers ranging from high (CSOD-derived) to medium (API
  metadata) to medium-low (document inferences)
- Full provenance for every finding back to its source

### 8.4 Findings hand-off to card generation

Card generation operators read from the bus. The handoff is clean: card
generation knows nothing about Spark, Salesforce APIs, or PDF chunking; it
only knows about typed findings. The Foundry has done its job — turned
heterogeneous sources into a uniform stream of typed, calibrated, traceable
findings.

```
[02:52:14] Card generation begins
  - 312 object_type cards (89 from Salesforce, 47 from CSOD, 23 from
    Cornerstone, 142 from CDM seeds, 11 new from documents)
  - 1894 property_type cards
  - 547 link_type cards
  - 89 derivation_rule cards
  - 70 validation_rule cards
  - 18 causal_rule cards
  - 142 causal_edge cards (47 active priors + 78 discovered + 17 from claims)
  - 47 governance cards
  - 23 marking cards
[03:07:42] Card generation complete
```

The Foundry handed off. From here, the card generation, graph maintenance,
eval, and HITL flows take over.

---

## 9. Key Design Decisions

A few decisions that shape the Foundry materially. Worth being explicit
about each.

### 9.1 Seed-anchored, not seed-bounded

The Foundry uses seed knowledge as an **anchor** but doesn't restrict
extractions to seed-known concepts. Documents can introduce new concepts;
schemas can have unmapped tables; APIs can have custom objects without CDM
correspondence. These flow to HITL with the seed as the comparison
baseline. This is the right balance: seed knowledge gives the system
ground without making it rigid.

### 9.2 Operators are deterministic in their contract

Given the same inputs and the same seed registry, an operator produces the
same output. LLM calls go through the model registry's cache, which keys
on inputs. This makes runs reproducible and incremental runs cheap.

### 9.3 Provenance is mandatory

Every finding carries derivation provenance. The cost is non-trivial in
storage (~30% of finding size); the value is non-negotiable for the eval
framework, the HITL queue, and the audit log. This is a hard requirement,
not a configurable feature.

### 9.4 Confidence tiers, not confidence scores

Findings carry tiers (`high`, `medium`, `low`), not numeric confidence.
Numeric confidence is too easy to invent and too easy to ignore. Tiers
force discrete decisions about how findings are treated downstream and
are visible to users in the response synthesis. Within a tier, the
provenance path tells the full story.

### 9.5 Source-mode-aware planning

The pipeline planner knows about source modes. It skips unsupported
operators rather than failing. This keeps multi-mode pipelines from
breaking when one source is unavailable. The cost is more planning logic;
the benefit is robust deployment in real customer environments.

### 9.6 The seed embedding index is run-local

Each pipeline run loads its own seed embedding index. It's not shared
across runs because pack versions and tenant overrides can change. The
load cost is small (seconds for ~10K seed entries) and the isolation
prevents cross-run contamination.

### 9.7 No new model categories without operator categories

The Foundry adds models conservatively. Each new model category should
correspond to a new operator category. Avoid adding models because they're
interesting; add them because an operator needs them.

---

## 10. Tooling

A consolidated table of what runs where.

| Concern              | Tool / Library                                              |
| -------------------- | ----------------------------------------------------------- |
| **Seed loader**      | Custom Pydantic-validated YAML loader                       |
| **Schema parsing**   | sqlglot (multi-dialect SQL parsing), dbt manifest reader     |
| **Document chunking** | unstructured.io, LlamaIndex node parsers, custom code AST   |
| **NER (generic)**    | spaCy (en_core_web_trf with GPU)                            |
| **NER (domain)**     | GLiNER initialized from pack types                          |
| **NER (medical)**    | MedCAT, ScispaCy (eClinical pack)                           |
| **Embedders**        | bge-large-en-v1.5 (default), text-embedding-3-large (alt)   |
| **LLM access**       | Anthropic SDK, OpenAI SDK, Ray Serve for self-hosted        |
| **API metadata**     | Custom adapters per system (Salesforce simple-salesforce, etc.) |
| **Profiling**        | DuckDB + scipy + custom HLL/t-digest implementations         |
| **Correlations**     | scipy, statsmodels, dython, phik                             |
| **Causal discovery** | causal-learn, lingam, notears                                |
| **Causal effects**   | DoWhy, EconML                                                |
| **Refutation**       | DoWhy refutation tests                                       |
| **Vector index**     | Qdrant (collections per scope)                               |
| **Findings bus**     | Postgres tables (standalone), Delta Lake (Spark), Ray Datasets |
| **Caching**          | Redis (hot) + Postgres (durable)                             |
| **Orchestration**    | LangGraph for sub-workflows, Dagster for top-level           |

---

## 11. Open Questions

1. **Seed registry size limits.** A pack with 50K seed concepts (full MedDRA,
   for instance) is feasible but slow to embed at startup. A pre-baked
   embedding index that ships with the pack is the obvious optimization.
   Worth designing once we have a real packs of that size.

2. **Operator parallelization within sources.** Within Stream 1 (CSOD
   warehouse), can the column profiler and correlator run concurrently
   per-table? Probably yes for distinct tables; not for a correlator that
   needs profiles as inputs. Probably worth a planner that infers DAG
   structure from operator inputs.

3. **Adapter quality.** The API metadata adapters need to be robust against
   API changes. Salesforce changes its API often; Cornerstone less so.
   Adapter versioning and deprecation policy is operationally important
   and not yet specified.

4. **Cost-aware operator gating.** An operator that calls an expensive LLM
   role per chunk on a 10K-chunk document is a budget hazard. The
   degradation strategy should include "downgrade role" not just "skip."
   Worth defining the downgrade ladder per role.

5. **Conflict resolution between source streams.** When Stream 1 and Stream
   2 both produce findings for the same target (csod.employee and
   salesforce.Contact both map to cdm.identity.user), what's the
   conflict-resolution policy? Probably both flow to card generation as
   distinct findings; card generation merges. Worth being explicit.

6. **Custom operators.** Customers writing their own Foundry operators (per
   the §14 questions in extraction design) need a contract that's stable
   across pack versions. Operator versioning and the contract guarantees
   are worth nailing down before opening the API.

7. **Streaming vs batch in the Foundry.** Currently batch-oriented. An
   outcome stream that arrives continuously wants streaming Foundry
   operators. Out of scope for the current design but worth flagging
   for the future.

8. **Run-local vs pack-shared seed embedding indices.** The current design
   loads per run. An alternative: ship pre-baked seed indices with packs,
   so runs only embed tenant-specific content. The right answer probably
   depends on pack size and how often packs vs tenants change.

---

## 12. Phased Delivery

**Phase 1 — Seed loader and registry.** Configuration loading, validation,
embedding index for seeds, basic operator scaffolding. End: a seed registry
loads from a pack and exposes lookup APIs. ~2 weeks.

**Phase 2 — Document extractors.** Chunkers, NER pipeline, claim extractors,
entity linkers — all seed-aware. End: documents flow through Foundry to
findings. ~4 weeks.

**Phase 3 — Schema extractors.** DDL parser, dbt manifest reader, schema
mapper with CDM alignment, property/FK extractors. End: schemas produce
structured findings anchored to CDM. ~3 weeks.

**Phase 4 — API metadata extractors.** Salesforce + Workday + ServiceNow
adapters. Object/property/relationship/validation/workflow extractors.
End: Mode B and Mode C deployments produce useful ontologies. ~4 weeks.

**Phase 5 — Data extractors.** Column profilers, correlators, FK validators
with degradation strategies. End: Mode A and Mode B data findings flow.
~3 weeks.

**Phase 6 — Causal extractors.** Causal prior matcher (highest leverage),
discovery suite, refutation tester. End: causal findings flow with seed
anchoring. ~3 weeks.

**Phase 7 — Outcome extractors.** Outcome streams with seed-concept tagging.
End: Weight Learner has labeled training data. ~2 weeks.

**Phase 8 — Source-mode planning and graceful degradation.** Pipeline
planner that consults operator contracts, skips unsupported operators,
manages the findings bus. End: production-ready multi-mode pipelines.
~2 weeks.

**Phase 9 — Custom operator API and adapter ecosystem.** Documented
operator contract, versioning, security model for customer-written
operators. End: customers can extend Foundry with proprietary adapters
and operators. ~3 weeks.

Total to GA: ~26 weeks for full Foundry. Phases 1-3 deliver a usable
document + schema pipeline (~9 weeks). Phase 4 brings the metadata-only
deployment story (~13 weeks). Phase 5+ deepens the data and causal stories.

The critical-path is Phases 1-4. Once the seed-aware extractors and API
metadata extractors are solid, the rest is incremental capability. A new
domain pack ships against this Foundry without Foundry changes — that's
the test of whether the abstraction is right.
