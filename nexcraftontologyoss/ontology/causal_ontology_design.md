# Causal Ontology Layer — Design

A four-layer ontology system that compiles a data model into a queryable causal
reasoning graph, with **natural-language knowledge cards as the source of truth**
and typed structure derived on demand.

---

## 1. Philosophy

Most ontology systems store typed objects, links, and rules in YAML or a relational
schema, with documentation written separately. The two drift apart, and the LLM
components that increasingly drive ontology work (semantic enrichment, causal
hypothesis generation, query planning) have to convert their natural medium —
prose — into typed records and back again.

This design inverts that. Every artifact in the ontology is a **knowledge card**:
a short prose document with a small structured header. Cards live in a vector
store. When a compiler, validator, or causal engine needs precise structured
data, it extracts that data from the cards at compile time. The cards stay
human-readable; the structure stays consistent because it is regenerated from a
single readable source.

### What we accept

- Slightly higher retrieval cost (one or two extra hops per query).
- Larger storage footprint (cards are bigger than rows).
- Validation at extraction time rather than author time.

### What we gain

- The ontology *is* the documentation. No drift.
- LLM components read and write their native medium.
- Causal claims are auditable in prose, with weights and evidence inline.
- New contributors can read the ontology without learning a schema.
- Versioning becomes natural — every card edit is a diff with rationale.

For the target scale (hundreds to low thousands of cards per tenant) the
retrieval cost is negligible. For the target use case (causal reasoning over
LMS and security data with governance constraints) the readability gain
compounds across every workflow.

---

## 2. The Four Layers

The system is organized into four layers. Each layer is a typed registry of
knowledge cards. Layers are additive: every Semantic-Layer card can carry
Kinetic-Layer effects, Dynamic-Layer markings, and Export-Bridge serialization
hints without leaking concerns across boundaries.

```
┌────────────────────────────────────────────────────────────┐
│  DYNAMIC LAYER                                             │
│  Roles • Markings • Permissions • Audit • Lineage          │
├────────────────────────────────────────────────────────────┤
│  KINETIC LAYER                                             │
│  Actions • Functions • Derivation Rules • Validation       │
│  Causal Rules • Effects                                    │
├────────────────────────────────────────────────────────────┤
│  SEMANTIC LAYER                                            │
│  Object Types • Link Types • Property Types • Interfaces   │
│  Concepts • Causal Nodes • Causal Edges                    │
├────────────────────────────────────────────────────────────┤
│  EXPORT BRIDGE                                             │
│  OWL • SHACL • OntoGuard • MCP                             │
└────────────────────────────────────────────────────────────┘
```

### 2.1 Semantic Layer — what exists

The Semantic Layer holds everything the data model knows about: entities,
relationships, properties, abstract concepts, and the causal nodes/edges that
link domain meaning to intervenable variables.

| Card kind     | Purpose                                                   |
| ------------- | --------------------------------------------------------- |
| `object_type` | An entity drawn from a source table or stream             |
| `link_type`   | A relationship — structural, temporal, derived, or causal |
| `property_type` | A field with type, range, semantics, units              |
| `interface`   | A contract that multiple object types implement           |
| `concept`     | A domain idea that is not row-bound                       |
| `causal_node` | A variable in the causal graph                            |
| `causal_edge` | A directed effect with weight, CI, and identifiability    |

The discipline that matters most here: **structural, temporal, derived, and
causal relationships all live in `link_type` with a `derivation` field that
distinguishes them.** Only edges with `derivation: causal` get a companion
`causal_edge` card carrying weights, confidence intervals, and identifiability
metadata. This keeps the graph store unified while letting the causal compiler
pull a clean DAG.

### 2.2 Kinetic Layer — what changes things

The Kinetic Layer holds everything that produces effects on the Semantic Layer
or on causal node states.

| Card kind         | Purpose                                                |
| ----------------- | ------------------------------------------------------ |
| `action_type`     | A user- or system-invoked operation that writes state  |
| `function`        | A pure compute — deterministic, SQL, Python, or LLM    |
| `derivation_rule` | Deterministic state extraction from columns/dates      |
| `validation_rule` | A SHACL-shaped invariant the data must satisfy         |
| `causal_rule`     | How a causal edge activates and contributes            |
| `effect`          | A reactive recompute hint when a state changes         |

Two design decisions worth pinning here. First, **`function` cards can be
LLM-backed**. A "classify this course domain" function is just a function whose
body is a prompt; it inherits the same audit/cache contract as deterministic
functions. Second, **`causal_rule` separates *activation* from *contribution***.
Activation says when an edge participates in a computation; contribution says
how much credit it gets via Shapley attribution. This mirrors the noisy-OR plus
Shapley split used in the CCE.

### 2.3 Dynamic Layer — who can do what, and what happened

The Dynamic Layer holds governance, access control, and provenance.

| Card kind       | Purpose                                                |
| --------------- | ------------------------------------------------------ |
| `marking`       | A classification (PII, restricted) with propagation    |
| `role`          | A bundle of permissions                                |
| `permission`    | A scoped grant — object type, action, marking filter   |
| `audit_entry`   | An append-only record of a write                       |
| `lineage_edge`  | A provenance link from derived value to source rows    |

Lineage is what turns this from a knowledge graph into something governable.
Every causal claim must eventually point back to a `lineage_edge` chain ending
in raw source rows. A `causal_edge` weight without a lineage trail is a
hypothesis, not a finding.

### 2.4 Export Bridge — same ontology, multiple wire formats

The Export Bridge is a set of pure compilers over the registries above. No
exporter writes back into the ontology.

| Exporter   | What it produces                                                |
| ---------- | --------------------------------------------------------------- |
| OWL        | `object_type → owl:Class`, `link_type → owl:ObjectProperty`, etc. |
| SHACL      | `validation_rule → sh:NodeShape` with property constraints       |
| OntoGuard  | `marking → og:Classification`, `permission → og:AccessRule`      |
| MCP        | `action_type` and `function` cards become MCP tools              |

Causal nodes and edges are not exported to OWL natively — the standard does not
carry weights or CIs cleanly. Instead they ship in a custom `cce:` namespace
with reified statements so the causal metadata survives.

---

## 3. The Knowledge Card

Every card in every layer has the same envelope: a small YAML header followed
by prose. The header carries the minimum structure the system needs to filter,
group, and version. The prose carries the meaning.

```
---
id: <stable_id>
layer: <semantic | kinetic | dynamic>
kind: <object_type | link_type | causal_edge | ...>
version: <integer>
refs: [<id>, <id>, ...]
<kind-specific keys>
---
<prose body — 100 to 400 words typically>
```

### 3.1 What goes in the header

The header is the place where structure does live. Two rules:

1. **Quantitative facts go in the header.** Anything that needs to be exact —
   weights, confidence intervals, sample counts, identifiability flags,
   cardinalities, derivation types — is a header field. The body explains
   them; the header is the source of truth for the value.
2. **Cross-card references go in the header.** A `refs` list of card IDs lets
   retrieval traverse the graph without parsing prose. The references in the
   body are for humans; the references in the header are for the compiler.

### 3.2 What goes in the body

The body is prose. It explains what the card represents, where it came from,
how to interpret it, and how it relates to neighbors. Bodies should be
self-contained enough that retrieving a single card gives a useful answer to a
narrow question, but they should not duplicate content already in linked cards.

Target length is 100 to 400 words. Cards above ~600 words should be split.

### 3.3 Header schemas per kind

Each card kind has a small validated header schema. Examples:

- `object_type` headers must declare: `id`, `version`, `refs`, optionally
  `extends` (interfaces) and `markings`.
- `link_type` headers must declare: `derivation` (one of `structural`,
  `temporal`, `derived`, `causal`, `governance`), `cardinality`, `confidence`.
- `causal_edge` headers must declare: `weight: { value, ci, n, source }`,
  `identifiability`, `effect`, `functional_form`.
- `derivation_rule` headers must declare: `produces`, the state it derives.
- `action_type` headers must declare: `inputs`, `output`, `audit`.
- `marking` headers must declare: `classification`, `propagation`.

The schemas are validated at write time. Free-form keys are allowed alongside
required ones.

### 3.4 Versioning

Every card edit creates a new version. The history is full versions plus
derived diffs:

- Full versions are easier for retrieval ("show me what this edge said in
  March").
- Diffs are derived on demand for review ("what changed between v3 and v4").

Each version carries a `rationale` field — a one-sentence reason for the
change. Causal edge weight updates carry a pointer to the evidence batch that
drove the update.

---

## 4. The Knowledge Engine

The Knowledge Engine is the pipeline that produces and tunes cards. Each stage
is a prompt-driven generator with deterministic guardrails — deterministic
extraction for keys, dates, statuses, and enums; LLM extraction for business
meaning, causal intent, and policy mapping.

```
Schema Profiler         → emits object_type cards from DDL/JSON-Schema
                          (deterministic structure, LLM writes the prose)
Structural Extractor    → emits link_type cards (derivation: structural)
                          from foreign key constraints
Temporal Extractor      → emits link_type cards (derivation: temporal)
                          from date columns
State Extractor         → emits derivation_rule cards from status/date logic
Semantic Enricher       → emits concept cards and softer link_type cards
                          via LLM over entity descriptions
Causal Hypothesizer     → emits causal_edge cards with weight.source: hypothesized
Identifiability Checker → updates causal_edge cards with identifiability prose
                          and flags admissibility in the header
Weight Learner          → fits causal_edge weights from outcome data and
                          rewrites the prose to reflect the new evidence
Validator               → reads cards, runs SHACL-equivalent checks, emits
                          validation_report cards (also natural language)
Tuner                   → consumes outcome signals, edits cards in place,
                          versioning prior text in history
```

Two non-obvious decisions:

1. **The Causal Hypothesizer is never the source of truth.** It produces
   *candidates* with `weight.source: hypothesized`. The Weight Learner promotes
   them to `weight.source: learned` only after enough observations
   (typically `n >= 5000` and CI width below 0.3, but this is policy that
   lives in a `causal_rule` card, not hard-coded).
2. **The Tuner needs an explicit feedback contract.** Which downstream signals
   (action outcomes, user corrections, A/B results) flow back into which
   edges? This is a `causal_rule` card per signal type, listing the edges it
   updates and the weight assigned to the signal.

---

## 5. Vector Store Organization

Single Qdrant cluster, multiple collections sharded by layer and kind. The card
body is embedded; the header is stored as Qdrant payload (filterable metadata).

```
ontology_semantic_objects        — object_type cards
ontology_semantic_links          — link_type cards (all derivations)
ontology_semantic_properties     — property_type cards
ontology_semantic_interfaces     — interface cards
ontology_semantic_concepts       — concept cards
ontology_semantic_causal_nodes   — causal_node cards
ontology_semantic_causal_edges   — causal_edge cards
ontology_kinetic_actions         — action_type cards
ontology_kinetic_functions       — function cards
ontology_kinetic_rules           — derivation, validation, causal rules
ontology_dynamic                 — marking, role, permission cards
ontology_lineage                 — lineage_edge cards
ontology_audit                   — audit_entry cards (append-only)
```

### Retrieval patterns

| Question                                                   | Pattern                                                      |
| ---------------------------------------------------------- | ------------------------------------------------------------ |
| "What do we know about overdue training risk?"             | Semantic search over `causal_edges` and `causal_nodes`        |
| "What actions affect employee compliance state?"           | Filter on `kinetic_actions` by `invalidates: employee.compliance_state` |
| "What is this employee marked with?"                       | Filter on `dynamic` by `id: pii_marking` plus card refs      |
| "How did this number get computed?"                        | Traverse `lineage_edge` cards from output backward           |
| "What changed about this edge in the last quarter?"        | Version history of the `causal_edge` card                    |

A reverse-index sidecar is maintained for `refs` so that "what cards reference
employee?" is a constant-time lookup rather than a scan.

---

## 6. KnowQL

KnowQL has two surfaces. The natural-language surface handles most queries via
retrieval. The structured surface handles the cases where precision matters —
causal effect estimation, counterfactual simulation, lineage traversal.

### 6.1 Natural-language surface

The planner retrieves relevant cards, decides whether the question can be
answered from card content alone, and either responds directly or compiles to
the structured surface.

```
> "How much does manager follow-up actually help with overdue training,
   and is that effect identifiable?"

[planner retrieves: manager_followup_reduces_overdue card,
                   identifiability cards for related edges,
                   confounder cards for ManagerEngagement and Role]
[planner decides: causal effect query with identifiability question,
                  no structured execution needed — answer from cards]

→ Response synthesized from card prose, with citations to card IDs.
```

### 6.2 Structured surface

Used when the query needs computation, not just retrieval.

```
# Pattern query — compiles to SQL/Cypher
MATCH (e:Employee)-[:assigned_to]->(ta:TrainingAssignment)-[:for_course]->(c:Course)
WHERE c.category = "Cybersecurity" AND ta.status = "overdue"
RETURN e, ta, c

# Causal query — compiles to do-calculus engine
CAUSAL EFFECT OF ManagerFollowup ON OverdueRisk
GIVEN Employee.role = "privileged"
USING BACKDOOR ADJUSTMENT

# Counterfactual — compiles to PyMC / DoWhy
WHAT-IF SET ReminderSent = true FOR Employee WHERE department = "Engineering"
RETURN expected ComplianceGap, attribution BY causal_edge

# Concept similarity — compiles to Qdrant + ontology join
FIND Concepts SIMILAR TO "phishing susceptibility"
WITH evidence FROM Employee.training_history
LIMIT 5

# Lineage — compiles to lineage_edge traversal
EXPLAIN Employee.compliance_state(emp_47)
TRACE TO source_rows
```

The four causal primitives are `CAUSAL EFFECT`, `WHAT-IF`, `ATTRIBUTE` (Shapley
over a target node), and `IDENTIFY` (does this query have a valid adjustment
set). Almost every other causal query composes from those.

### 6.3 Routing

```
NL question
  ↓
Planner retrieves relevant cards
  ↓
Can the cards answer directly?  ─── yes ──► Respond with prose + citations
  ↓ no
Compile to structured KnowQL
  ↓
Execute on appropriate backend
  (warehouse / Qdrant / causal engine / lineage store)
  ↓
Apply Dynamic Layer filtering (markings, permissions)
  ↓
Synthesize response
```

Every query, regardless of surface, runs through the Dynamic Layer for
permission and marking filtering before results return.

---

## 7. Causal Reasoning Through the Stack

The chain that makes this design earn its keep:

```
raw source rows               ← Dynamic.lineage roots
  → object_type instances      ← Semantic Layer materialization
  → link_type edges            ← Structural / Temporal extractors
  → derivation_rule outputs    ← Kinetic Layer, deterministic
  → concept attachments        ← Semantic Enricher
  → causal_node observations   ← mapped from object_type properties
  → causal_edge activations    ← causal_rule firing, with Shapley contributions
  → KnowQL CAUSAL queries      ← planner over causal graph + identifiability check
```

Every step is a card. Every card has lineage back to the rows that produced it.
When a `causal_edge` weight updates, you can answer "which 14,820 observations
supported this update, sourced from which raw rows, governed by which markings"
in a single traversal.

---

## 8. Design Principles

1. **Causal-first.** The causal layer is not an afterthought. `causal_node` and
   `causal_edge` are first-class card kinds, with their own retrieval
   patterns, weight learning loop, and identifiability metadata.
2. **Lineage-bound.** No card without a lineage chain. Hypothesized causal
   edges are explicitly flagged so they can never silently become "facts".
3. **Versioned in place.** Cards evolve. History is a first-class queryable.
4. **Provenance-traceable.** Every value, weight, and inference points back
   to the rows or cards that produced it.
5. **Readable by default.** If a card is unreadable, it is broken. Prose is
   the contract.

---

## 9. Open Design Questions

1. **Card length cap.** 600 words is the working ceiling. Do we enforce hard?
   Soft warning is probably better; some causal edges with rich identification
   stories legitimately need more room.
2. **History format.** Full versions plus derived diffs is the working answer.
   Storage cost is the only concern — measurable once we have a real corpus.
3. **Header schema strictness.** Required keys per kind, free-form for the
   rest. Validated at write time, re-validated on read by the extractor.
4. **Cross-card reference resolution.** Reverse index maintained alongside
   primary store. Eventual consistency on writes, strongly consistent on
   reads after a small delay.
5. **Single store vs split for causal traversal.** Postgres + Qdrant works for
   most patterns. If `causal_edge` traversal exceeds ~10k edges per tenant,
   adding a graph store (KuzuDB or Neo4j) for the causal subgraph is a clean
   incremental step.
6. **Hypothesized → learned promotion threshold.** Lives in a `causal_rule`
   card, not hard-coded. Default `n >= 5000`, CI width below 0.3, sign
   stable across last three updates.
7. **Interface granularity.** Start with 6–8 broad interfaces (`Trainable`,
   `Auditable`, `Markable`, etc.). Resist further decomposition until pain
   forces it.

---

## 10. What This Looks Like in Practice

See the companion file `causal_ontology_example_slice.md` for a worked example:
the `Employee → TrainingAssignment → LateCompletion → OverdueRisk` slice with
all four layers populated.
