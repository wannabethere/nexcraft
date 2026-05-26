# Knowledge Engine — Ingestion Pipeline Design

The pipeline that takes raw source signals (schemas, statistics, correlations,
business documents, outcome data) and produces or updates knowledge cards in
the vector store. Runs daily, incrementally, idempotently. Co-evolves cards
with their source data.

---

## 1. Goals and Constraints

The pipeline must satisfy six constraints that pull in slightly different
directions, and the design negotiates between them.

1. **Incremental.** A daily run touches only what changed. The corpus may have
   thousands of cards; regenerating all of them every night is wasteful and
   thrashes versions. Cost should scale with the change set, not the corpus.
2. **Idempotent.** Running the same day twice produces the same result. No
   duplicates, no version churn from re-runs.
3. **Multi-source.** Schemas, profiling stats, correlation matrices, business
   documents, query logs, and outcome data all feed the same card store.
4. **Auditable.** Every card edit traces back to the run, the input that
   triggered it, and the rule that produced it.
5. **Reversible.** A bad run can be rolled back per-card without restoring
   from backup.
6. **Cost-aware.** LLM calls dominate cost. Cache aggressively, regenerate
   only what changed, batch where possible.

The first run is a special case — every input is "new", so the pipeline
behaves as a bootstrap. Every subsequent run is incremental. The same code
handles both; backfill is just the degenerate case of "everything changed".

---

## 2. Inputs

The pipeline consumes seven input categories. The first three were specified;
the next four are the additions worth pulling in early because they
disproportionately improve card quality.

### 2.1 Specified inputs

| Input                  | What it gives                                              |
| ---------------------- | ---------------------------------------------------------- |
| **Data model stats**   | Tables, columns, types, FKs, null rates, distinct counts, value ranges, cardinality |
| **Correlation analysis** | Pairwise dependencies between columns — strength, direction, type (linear, monotonic, non-linear) |
| **Business documents** | Policies, runbooks, training materials, compliance specs, design docs |

### 2.2 Recommended additional inputs

| Input                  | What it gives                                              |
| ---------------------- | ---------------------------------------------------------- |
| **Causal structure discovery** | Beyond pairwise: PC algorithm, FGES, LiNGAM, NOTEARS — produces a candidate DAG that the Causal Hypothesizer ranks against |
| **Query/BI logs**      | Which entities and joins are actually used, how often, by whom — drives importance ranking and link confidence |
| **Outcome data**       | Labels for causal weight learning — did the predicted overdue actually happen, did the intervention work |
| **Code repositories**  | Domain logic encoded in validation rules and ETL — often the only source of "what does 'active employee' actually mean" |

The four additions roughly double the quality of generated cards because
they fill gaps that schemas and statistics alone leave open. Causal structure
discovery moves causal hypotheses from "the LLM guessed" toward "the data
suggests"; query logs tell you which links matter; outcome data is what
turns hypothesized weights into learned ones; code repos resolve the
"business meaning" of fields that schemas can't capture.

### 2.3 Optional inputs worth wiring in if available

- **dbt manifest / lineage exports** — column-level lineage for free, used
  to populate `lineage_edge` cards without inference.
- **Existing data catalogs** — Atlan, Collibra, dbt docs, AWS Glue — already
  carry descriptions and tags; treat as authoritative seeds.
- **Domain ontologies** — for security: CWE, CAPEC, ATT&CK, OWASP LLM Top 10.
  For LMS: SCORM, xAPI, IMS Caliper. These give canonical concept hierarchies
  the system can attach to.
- **Sample query traces / LLM-call logs** — what questions are asked of the
  ontology — a feedback signal for what cards need to be richer.
- **User edits and corrections** — when a human edits a card, that edit is
  the strongest possible signal and should propagate.

---

## 3. Architecture

The pipeline is six layers, each a clear stage with defined inputs, outputs,
and idempotency guarantees.

```
┌──────────────────────────────────────────────────────────────────┐
│  SOURCES                                                         │
│  Warehouse │ Catalogs │ Documents │ Code │ Logs │ Outcomes       │
└──────────────────────────────────────────────────────────────────┘
                              │
                              ▼
┌──────────────────────────────────────────────────────────────────┐
│  1. ACQUISITION                                                  │
│  Schema fetchers │ Profilers │ Doc loaders │ Outcome collectors  │
│  Output: typed source artifacts with content hashes              │
└──────────────────────────────────────────────────────────────────┘
                              │
                              ▼
┌──────────────────────────────────────────────────────────────────┐
│  2. ANALYSIS                                                     │
│  Correlation │ Causal-structure │ Claim extraction │ NER         │
│  Output: structured findings with provenance                     │
└──────────────────────────────────────────────────────────────────┘
                              │
                              ▼
┌──────────────────────────────────────────────────────────────────┐
│  3. CHANGE DETECTION                                             │
│  Manifest diff │ Affected-card resolution                        │
│  Output: change set — list of (card_id, change_reason)           │
└──────────────────────────────────────────────────────────────────┘
                              │
                              ▼
┌──────────────────────────────────────────────────────────────────┐
│  4. CARD GENERATION (LangGraph)                                  │
│  Per-card planner │ Evidence retrieval │ Draft │ Validate │ Diff │
│  Output: candidate card versions                                 │
└──────────────────────────────────────────────────────────────────┘
                              │
                              ▼
┌──────────────────────────────────────────────────────────────────┐
│  5. REVIEW & PROMOTION                                           │
│  Quality gates │ Confidence routing │ HITL queue                 │
│  Output: approved card versions                                  │
└──────────────────────────────────────────────────────────────────┘
                              │
                              ▼
┌──────────────────────────────────────────────────────────────────┐
│  6. PUBLISH                                                      │
│  Embed │ Vector store │ Reverse index │ Audit │ Notifications    │
│  Output: live card versions, queryable                           │
└──────────────────────────────────────────────────────────────────┘
```

Each layer reads from the previous and writes to a typed artifact store keyed
by `run_id`. A run is a top-level transaction: all six stages either complete
together or the run is rolled back.

---

## 4. Stage 1 — Acquisition

### 4.1 What it does

Fetches every source signal, normalizes it to a typed source artifact, and
content-hashes it for change detection.

| Source           | Acquisition                                          | Cadence  |
| ---------------- | ---------------------------------------------------- | -------- |
| Schema (DDL)     | Information schema queries, dbt manifest             | Daily    |
| Column profiling | DuckDB / Trino sampled scans                         | Daily    |
| Distributions    | DuckDB approximate quantiles, HLL distinct counts    | Daily    |
| Documents        | Source folder watch, S3/Drive pull, Git pull         | Daily    |
| Code repos       | Git pull, language-aware static analysis             | Daily    |
| Query logs       | Warehouse query history, BI metadata                 | Daily    |
| Outcome data     | Application event streams, labeled outcome tables    | Daily    |

### 4.2 Tools

| Need                          | Recommendation                                       |
| ----------------------------- | ---------------------------------------------------- |
| Schema introspection          | SQLAlchemy reflection, dbt manifest parser           |
| Profiling (lightweight)       | `whylogs`, `Soda Core`                               |
| Profiling (rich, ad-hoc)      | `ydata-profiling` (`pandas-profiling`)               |
| Profiling at scale            | DuckDB scan + custom aggregations (Sameer's pattern) |
| Document loading              | `unstructured.io`, LlamaIndex readers                |
| PDF layout                    | `unstructured.io`, `pdfplumber` for tables           |
| Code analysis                 | `tree-sitter`, language-specific AST tools           |
| Query log access              | Warehouse-specific (Snowflake QUERY_HISTORY, etc.)   |

### 4.3 Output contract

Every acquired artifact is written to a run-scoped staging area as:

```
artifacts/<run_id>/<source_type>/<artifact_id>.json
artifacts/<run_id>/<source_type>/<artifact_id>.hash
```

The hash is SHA-256 of the canonical JSON. This is the change-detection
primitive used by Stage 3.

### 4.4 Incremental hooks

- **Schemas**: hash full DDL per table. If unchanged, no downstream work for
  that table's `object_type` card.
- **Profiling**: store last-known-good profile. Only re-profile tables with
  data changes (use warehouse change-time metadata or row-count deltas).
- **Documents**: hash content and metadata (mtime, size). Skip embedding if
  hash unchanged.
- **Outcomes**: time-windowed; pull only events since last watermark.

---

## 5. Stage 2 — Analysis

### 5.1 What it does

Turns acquired artifacts into structured findings that downstream card
generation consumes. Four sub-stages, each independent and parallelizable:
correlation, causal structure discovery, **named-entity recognition (NER)**,
and **claim extraction (LLM grounded on NER output)**.

NER and claim extraction run as a hybrid because each on its own falls
short. NER reliably enumerates *what* entities and types are present in a
document but says nothing about meaning. The LLM is fluent at meaning but
hallucinates entities and references. Pairing them — NER finds the anchors,
the LLM grounds claims to those anchors — is materially better than either
alone, and meaningfully cheaper than running the LLM with a wider mandate.

### 5.2 Correlation analysis

Pairwise dependency detection across columns, by type:

| Column types         | Method                                                |
| -------------------- | ----------------------------------------------------- |
| Numeric ↔ Numeric    | Pearson (linear), Spearman (monotonic), distance correlation (non-linear) |
| Categorical ↔ Categorical | Cramér's V, Theil's U                            |
| Mixed                | `phik` (mixed-type correlation), correlation ratio η  |
| Any ↔ Any            | Mutual information (via `sklearn.feature_selection.mutual_info_*`) |
| Time-series          | Granger causality, cross-correlation, transfer entropy|

Outputs a correlation matrix per table-pair, with method, statistic, p-value,
and sample size. High-strength pairs across tables (joined via FKs) are
candidate `link_type` reinforcements; high-strength pairs within a table
suggest derived properties or hidden relationships.

### 5.3 Causal structure discovery

Beyond pairwise: candidate DAGs the Causal Hypothesizer can rank.

| Algorithm | When to use                                          | Library          |
| --------- | ---------------------------------------------------- | ---------------- |
| PC        | Default for observational data with no time order    | `causal-learn`   |
| FGES      | Larger graphs, score-based, scales better            | `causal-learn`, `py-causal` |
| LiNGAM    | When you can assume non-Gaussian noise               | `lingam`         |
| NOTEARS   | Continuous, differentiable, works with neural priors | `notears`        |
| Granger   | Time-series with clear temporal ordering             | `statsmodels`    |
| DoWhy refutation | Sanity-check candidate edges for robustness   | `dowhy`          |

Run multiple algorithms, intersect their outputs, surface the high-agreement
edges as strong candidates and disagreements as discussion points. The
Causal Hypothesizer in Stage 4 reads these and proposes `causal_edge` cards.

### 5.4 Named-entity recognition (NER)

NER runs first across document chunks, sample values, schema comments, and
profile reports. Its job is to enumerate every entity that might become a
card or already is one — typed, with character offsets, with confidence.

The entity types the system recognizes are domain-specific, not generic:

| Type                  | Examples                                                  |
| --------------------- | --------------------------------------------------------- |
| `entity_name`         | Employee, TrainingAssignment, Course                      |
| `attribute`           | progress_percent, due_date, employment_status             |
| `concept`             | LateCompletion, CompliancEvent, PhishingRisk              |
| `event`               | "completed late", "marked overdue", "assigned"            |
| `actor_role`          | HR Compliance Officer, Manager, Security Analyst          |
| `policy_reference`    | "Section 4.2", "ISO 27001 A.7.2.2", "SOC 2 CC6.1"         |
| `quantitative_claim`  | "40% reduction", "within 30 days", "p < 0.05"             |
| `temporal_qualifier`  | "weekly", "after hire", "before due date"                 |
| `causal_marker`       | "reduces", "increases", "leads to", "prevents"            |

The custom type set is what makes NER more than a generic preprocessor. A
"causal_marker" hit is a strong signal that the surrounding text contains a
candidate `causal_edge`. A "policy_reference" hit links a `validation_rule`
to its authority. A "quantitative_claim" hit pairs with a causal_marker to
build the weight prior on a hypothesized edge.

Tooling for the NER stage is intentionally hybrid:

| Need                       | Recommendation                                          |
| -------------------------- | ------------------------------------------------------- |
| Generic NER (people, orgs, dates) | spaCy with a strong pretrained pipeline           |
| Domain-typed zero-shot NER | **GLiNER** — accepts a list of types at inference time, no training data needed |
| Higher-accuracy domain NER | Fine-tuned encoder model (BERT/Flair) once labeled data accumulates |
| Causal markers             | Rule-based + GLiNER — the lexicon is small and stable    |
| Cross-document entity normalization | Embedding-based clustering on canonical forms   |

GLiNER is the workhorse. Its zero-shot regime fits the pipeline's posture —
the type set evolves as the ontology grows, and we don't want to retrain a
model every time a new card kind is introduced. spaCy supplements it for
the entity types where pretrained accuracy beats zero-shot.

### 5.5 Claim extraction (LLM, grounded on NER)

Once NER has produced the entity inventory for a chunk, the LLM is asked
to extract claims with the entities as constraints rather than as free-form
inferences. The prompt is structured roughly as:

> *Here is a document chunk. Here is the list of typed entities NER found
> in it, with offsets. Extract claims from this chunk. Every claim must
> reference at least one entity from the NER list. Do not invent entities
> not in the list. For each claim, return the type, the supporting span,
> and the entities involved.*

This grounding produces four kinds of claim, each tied directly to a
candidate card kind:

- **Definitions**: "An active employee is one whose status is not terminated
  and whose end_date is null." → candidate property semantics on
  `employment_status` and `end_date`.
- **Rules**: "Mandatory cybersecurity training must be completed within 30
  days of hire." → candidate `derivation_rule` or `validation_rule`,
  parameterized on the temporal_qualifier ("within 30 days") and the
  policy_reference if one was tagged.
- **Causal claims**: "Phishing simulation training reduces successful phishing
  attempts by ~40% in our environment." → candidate `causal_edge` from
  PhishingTraining to PhishingIncidentRate, weight prior 0.4,
  source: hypothesized.
- **Governance**: "Only HR Compliance Officers may modify training due dates."
  → candidate `permission` keyed on the actor_role and the action.

Each extraction carries provenance back to the document chunk and the NER
spans it relied on. The Card Generation stage ranks claims by source
authority — a policy document beats a training slide deck — and uses NER
spans as evidence anchors when humans review the resulting card.

The benefit of grounding the LLM on NER output, beyond reduced
hallucination, is that **every claim has resolvable references**. A claim
that mentions PhishingRisk gets a candidate `refs` list automatically; a
claim that mentions "the manager" is flagged for entity linking before it
can become a card.

### 5.6 Entity linking

NER finds entities; entity linking maps them to existing cards (or marks
them as candidates for new cards). This sub-stage runs after NER and before
claim extraction is finalized.

For each NER span, the linker:

1. Looks up the canonical form against the reverse-reference index by exact
   match. "TrainingAssignment" → `training_assignment` card.
2. If no exact match, embeds the span plus its surrounding sentence and
   queries the relevant Qdrant collection (`object_type` for entity_name
   spans, `concept` for concept spans, etc.). Top-3 nearest cards above a
   similarity threshold are candidates.
3. If still no match above threshold, the span becomes a **new entity
   candidate** — flagged for HITL review on the next run. The original
   claim is preserved but cannot publish until the entity is resolved.

The linker is what handles the "manager / supervisor / team lead"
normalization problem: three different surface forms across documents,
one canonical card. It is also what discovers genuinely new concepts that
deserve cards — when a domain term consistently fails to link across many
documents, it is signal that the ontology is missing something.

| Need                          | Recommendation                                       |
| ----------------------------- | ---------------------------------------------------- |
| Embedding for linking         | Same model as card body embeddings, query with span+context |
| Similarity threshold tuning   | Per-card-kind; concepts tolerate looser matches than object types |
| Candidate entity queue        | Append to HITL with span, sentence, top-3 near-matches |
| Cross-document deduplication  | Cluster spans before linking to reduce queue volume  |

### 5.7 Tools for analysis (consolidated)

| Need                          | Recommendation                                       |
| ----------------------------- | ---------------------------------------------------- |
| Correlation suite             | `scipy.stats`, `phik`, `dython`                      |
| Causal structure              | `causal-learn`, `dowhy`, `lingam`                    |
| Time-series causality         | `statsmodels`, `tigramite`                           |
| NER (generic types)           | `spaCy` with strong pretrained pipeline              |
| NER (domain-typed, zero-shot) | `GLiNER`                                             |
| Causal-marker detection       | Rule-based lexicon + GLiNER                          |
| Claim extraction              | LLM with Pydantic structured output, NER-grounded    |
| Entity linking                | Embedding similarity over Qdrant, per-kind thresholds |
| Document chunking             | `LlamaIndex` node parsers, `LangChain` text splitters |

### 5.8 Output contract

Findings written to `artifacts/<run_id>/findings/`:

```json
{
  "finding_id": "corr_emp_progress_overdue_2026-04-15",
  "type": "correlation",
  "method": "spearman",
  "subject": ["training_assignment.progress_percent", "training_assignment.is_overdue"],
  "statistic": -0.51,
  "p_value": 1.2e-18,
  "n": 14820,
  "provenance": {"source": "warehouse://csod.training_assignment", "run_id": "..."}
}
```

Findings are first-class artifacts. They survive across runs and accumulate as
evidence for causal edges. The Weight Learner reads them; the Causal
Hypothesizer reads them; humans reviewing card edits read them.

---

## 6. Stage 3 — Change Detection

### 6.1 What it does

Compares the current run's manifest of input hashes to the previous run's
manifest. Produces a **change set**: a list of `(card_id, change_reason,
triggering_input)` tuples.

### 6.2 The manifest

A run manifest is the complete fingerprint of all inputs that produced the
ontology state. Schematically:

```yaml
run_id: 2026-05-06T03:00:00Z
inputs:
  schemas:
    csod.employee:        { hash: sha256:..., version: 47 }
    csod.training_assignment: { hash: sha256:..., version: 92 }
  profiles:
    csod.employee:        { hash: sha256:..., row_count: 14201 }
  documents:
    /policies/training_2026.md: { hash: sha256:..., size: 4821 }
  findings:
    corr_emp_progress_overdue: { hash: sha256:..., n: 14820 }
```

Stored in `artifacts/<run_id>/manifest.json`. Comparing two manifests gives
the diff: added, removed, changed.

### 6.3 From input changes to affected cards

This is the non-trivial part. A change to one input may affect many cards
through the reference graph. The resolver works in two passes:

**Pass 1 — direct.** For each changed input, look up its directly bound cards.
A schema change for `csod.employee` directly affects the `employee`
`object_type` card.

**Pass 2 — transitive.** For each directly affected card, walk the reverse
reference graph one or two hops. A change to the `employee` object_type may
affect `link_type` cards that point at it, and via them, `causal_edge` cards
that depend on those links.

The walk is bounded — typically two hops, configurable. Beyond two hops the
effect is usually too diluted to be worth regenerating. Causal edges are an
exception: weight changes propagate through causal_rule activations, and the
walk follows causal edges as long as the weight-change magnitude exceeds a
threshold.

### 6.4 Change reasons

Each entry in the change set carries a reason, which Stage 4 uses to decide
*how* to regenerate:

| Reason                           | Implication for regeneration                       |
| -------------------------------- | -------------------------------------------------- |
| `schema_changed`                 | Header may need updates; body if structure shifted |
| `profile_changed_significantly`  | Cardinality/range mentions in body need refresh    |
| `document_added` / `_changed`    | New claims to merge into relevant cards            |
| `correlation_strength_changed`   | Confidence on link or causal edge needs update     |
| `outcome_observation_added`      | Causal edge weight may need refit                  |
| `manual_override`                | Human edit — propagate but don't overwrite         |
| `dependency_changed`             | Transitive — minimal regeneration, version bump    |

### 6.5 Output

```json
{
  "run_id": "...",
  "change_set": [
    {"card_id": "employee", "reason": "schema_changed", "input": "csod.employee@v47"},
    {"card_id": "low_progress_increases_overdue_risk", "reason": "outcome_observation_added", "input": "outcomes://2026-04"},
    {"card_id": "training_assignment", "reason": "dependency_changed", "input": "..."}
  ]
}
```

If the change set is empty, the pipeline short-circuits — no regeneration, no
new card versions, just a manifest snapshot for the historical record.

---

## 7. Stage 4 — Card Generation (LangGraph)

### 7.1 What it does

For each entry in the change set, generates a candidate new version of the
affected card. Each card kind has its own generator graph, sharing a common
skeleton.

### 7.2 The per-card generator graph

A LangGraph workflow per card kind, all following the same shape. NER is
woven into two of the steps — once on input (to anchor the LLM's draft on
real entities) and once on output (to validate the draft references resolve
to actual cards).

```
            ┌──────────────┐
            │ Load context │   prior version + neighbors + relevant findings
            └──────┬───────┘
                   ▼
            ┌──────────────┐
            │  NER on context │   extract entities the new card should anchor on
            └──────┬───────┘
                   ▼
            ┌──────────────┐
            │  Plan edits  │   what needs to change in this card and why
            └──────┬───────┘
                   ▼
            ┌──────────────┐
            │ Draft prose  │   LLM writes body, constrained to NER-found entities
            └──────┬───────┘
                   ▼
            ┌──────────────┐
            │  NER on draft │   extract entities mentioned in the new body
            └──────┬───────┘
                   ▼
            ┌──────────────┐
            │ Resolve refs │   link draft entities to existing cards;
            │              │   auto-populate refs[] in header
            └──────┬───────┘
                   ▼
            ┌──────────────┐
            │   Validate   │   schema, refs resolve, length cap, no contradictions
            └──────┬───────┘
                   ▼
        validation passes? ──── no ──► retry with feedback (max 3)
                   │ yes
                   ▼
            ┌──────────────┐
            │  Diff & version │  produce v_n+1 with rationale
            └──────────────┘
```

Two things to call out about the NER passes:

1. **NER on context** runs against the prior card version, neighboring cards,
   and any findings in the change set. The LLM's draft prompt includes the
   resulting entity list as "you may reference these entities by name; you
   may not invent new entity names." This is the primary anti-hallucination
   guardrail.
2. **NER on draft** is the validation layer. Every entity name found in the
   generated body must either link to an existing card (becomes a `refs`
   entry) or be flagged as a new entity candidate routed to the HITL queue.
   A draft with unresolvable entities cannot publish.

The `refs` header field is therefore **auto-populated**, not author-written.
The LLM writes prose; NER extracts the entities; entity linking maps them
to card IDs; the header is generated from the link set. The author of a
manually-edited card can override this, but the default path keeps refs in
sync with the body without manual bookkeeping.

Generators differ by what they pull as context and how they validate.

### 7.3 Generator types

| Card kind        | Specific concerns                                            |
| ---------------- | ------------------------------------------------------------ |
| `object_type`    | Structure from DDL is authoritative; LLM only writes prose for fields it can't infer. Header updates strict-validated. |
| `link_type`      | Derivation field constrained: structural from FK, temporal from date columns, derived from rules, causal only via Causal Hypothesizer. |
| `property_type`  | Stats from profiling pinned in header; semantics from docs and column comments. |
| `concept`        | Free-form prose, but must reference at least one `object_type` or `property_type`. Embedding-similarity check against existing concepts to avoid duplicates. |
| `causal_node`    | Variable type and prior must be specified explicitly; intervenable flag required. |
| `causal_edge`    | **Most constrained.** New edges always start as `weight.source: hypothesized` regardless of LLM's confidence. Identifiability is computed by the checker, never asserted by the LLM. Weight values flow only from the Weight Learner. |
| `derivation_rule` | Must be expressible as deterministic logic over named columns; LLM drafts the prose, but the rule body is structurally validated. |
| `action_type`    | Inputs and output must reference real `object_type` cards. Audit field always required. |
| `marking`        | Propagation rules must round-trip through the marking propagator without contradiction. |

### 7.4 Cost controls

- **Cache by content hash.** If the inputs haven't changed and the prior
  version's hash matches, skip generation entirely.
- **Batch by kind.** Generate all `object_type` cards in one batch with a
  shared system prompt to amortize tokens.
- **Use cheaper models for low-stakes cards.** A property_type card update
  can use a small/fast model; a causal_edge update should use the strongest
  model available.
- **Parallelize across cards.** No sequential dependency among same-kind
  cards. Cap concurrency to avoid rate-limit thrashing.

### 7.5 Tools

| Need                      | Recommendation                                       |
| ------------------------- | ---------------------------------------------------- |
| Workflow orchestration    | LangGraph (your existing pattern)                    |
| LLM calls                 | Anthropic API with structured output (Pydantic)      |
| NER (anchor + validate)   | GLiNER for domain-typed zero-shot, spaCy for generic types |
| Entity linking            | Embedding similarity over Qdrant, per-kind thresholds |
| Embedding for similarity  | `text-embedding-3-large` or `bge-large-en` for cost  |
| Validation                | Pydantic for headers, custom validator for refs      |
| Diff generation           | `difflib` for prose, structured diff for headers     |

---

## 8. Stage 5 — Review and Promotion

### 8.1 What it does

Decides which candidate card versions are auto-published versus routed to
human review.

### 8.2 Quality gates

Every candidate must pass before publication:

| Gate                          | Failure handling                                    |
| ----------------------------- | --------------------------------------------------- |
| Header schema valid for kind  | Fail run; return for regeneration                   |
| All `refs` resolve            | Fail run; missing refs may indicate orphans         |
| Body within length cap        | Warn at 600 words, fail at 1000                     |
| No contradiction with neighbors | LLM-checked: does this card contradict claims in linked cards? |
| Embedding-similarity check    | New concepts >0.92 cosine to existing → dedupe candidate |
| Causal edge identifiability   | Header must match Identifiability Checker output    |
| Weight change ≤ 2σ            | Larger changes route to HITL                        |

### 8.3 Confidence-based routing

Low-confidence or high-stakes changes go to a review queue rather than
auto-publish:

| Trigger                                          | Route                |
| ------------------------------------------------ | -------------------- |
| New top-level concept                            | HITL — concept review |
| New `causal_edge` (always hypothesized initially)| HITL — causal review |
| Causal edge weight change > 2σ                   | HITL — causal review |
| Permission or role card change                   | HITL — security review |
| Manual override on a card                        | Auto-publish (human is source) |
| Routine schema/profile updates                   | Auto-publish         |
| Validation passed, similarity check clean        | Auto-publish         |

The HITL queue is a separate read surface — reviewers see the prior version,
the candidate version, the diff, and the triggering inputs. They can accept,
reject, or edit-and-accept.

### 8.4 Promotion of hypothesized causal edges

A separate sub-pipeline runs against the causal edge corpus weekly:

```
For each causal_edge with weight.source = hypothesized:
    if n >= threshold(card.kind_specific_threshold):
       and CI_width <= 0.3:
       and sign stable across last 3 evidence batches:
        promote weight.source to "learned"
        increment version
        log promotion event
        notify reviewers
```

Thresholds live in `causal_rule` cards, not hard-coded. Promotion is the
moment a causal claim becomes a "finding" rather than a "guess", so it's
gated and logged.

---

## 9. Stage 6 — Publish

### 9.1 What it does

Atomically commits approved card versions to the live ontology.

### 9.2 Steps per card

1. **Embed body** with the configured embedding model.
2. **Write to vector store** in the appropriate Qdrant collection.
3. **Update reverse-reference index** so neighbor lookups find the new version.
4. **Append audit entry** with run_id, inputs, generator hash, reviewer (if HITL).
5. **Notify subscribers** (Slack, webhook, email) for high-stakes changes.

### 9.3 Atomicity

Publishing happens inside a logical transaction per card. Either the embed,
write, index update, and audit are all committed, or none are. The simplest
implementation: write to a staging area, validate, then perform a single
"version pointer" update that flips the live version atomically.

A run is considered complete when the manifest snapshot is committed to the
runs table. If any stage fails mid-run, the partial work is left in staging
and the next run picks up from there.

### 9.4 Notifications

Subscribers register interest by card kind, layer, or card ID. Useful
defaults:

- Compliance team subscribes to all `permission` and `marking` changes.
- Data platform team subscribes to all `object_type` and schema-related changes.
- ML team subscribes to `causal_edge` weight changes and promotions.

---

## 10. Incremental Execution — End-to-End

A worked example of what a daily run does when nothing dramatic happened.

```
03:00  Run starts. run_id assigned.
03:00  Stage 1 — Acquisition.
       - 312 schemas pulled, 311 hashes match previous, 1 changed
         (csod.training_assignment now has new column 'reminder_count')
       - 4 profile recomputes triggered by row-count delta
       - 2 documents added to /policies/, 1 modified
       - Outcome data: 8,400 new training events since last watermark
03:08  Stage 2 — Analysis.
       - Correlations recomputed for 4 profile-changed tables
       - Causal structure: PC algorithm re-run on changed slice
       - Document claim extraction on 3 new/changed docs (LLM)
03:14  Stage 3 — Change detection.
       - Manifest diff produces 17 affected cards:
         · 1 schema-changed (training_assignment)
         · 4 profile-changed (object_type and property_type)
         · 8 document-driven (concepts and rules from new policies)
         · 4 outcome-driven (causal_edge weight refits)
03:14  Stage 4 — Generation. 17 cards generated in parallel.
03:18  Stage 5 — Review.
       - 14 cards pass all gates, auto-published
       - 2 cards routed to HITL (causal edge weight changes > 2σ)
       - 1 card routed to HITL (new concept from policy doc)
03:18  Stage 6 — Publish.
       - 14 cards embedded, indexed, audited, notifications sent
       - Manifest snapshot committed
03:19  Run complete. Total LLM cost: $4.20.
```

The HITL items wait in queue. They do not block the run. The next morning a
reviewer either accepts (publishes), rejects (the candidate is discarded but
the trigger remains for the next run), or edits-and-accepts (the human edit
becomes the published version).

---

## 11. Handling Oversized Inputs and Outputs

Cards are 100–400 words by design. Documents arrive at arbitrary size.
Findings can run to thousands of rows. Card neighborhoods can be densely
connected. The pipeline needs consistent strategies for each of these so
nothing blows the context window or the storage budget.

Two principles cover most cases:

1. **Split by aspect, link by ref.** Anything too large for one card
   becomes multiple cards that reference each other. The vector store
   handles "find the related parts" via refs and embedding similarity at
   query time.
2. **Tier context by relevance.** When pulling neighbors for generation or
   retrieval, full body for the nearest few, summary for the next ring,
   IDs only beyond. The LLM rarely needs every neighbor's full prose.

The rest of this section applies these principles to each part of the
pipeline that has a sizing concern.

### 11.1 Source document chunking (Stage 1)

Document chunking strategy depends on document type — a generic recursive
text splitter wastes information that a structure-aware splitter preserves.

| Document type             | Chunking approach                                              |
| ------------------------- | -------------------------------------------------------------- |
| Policy / compliance docs  | Section-based, header-aware (use Markdown / heading levels)    |
| Technical architecture    | Section + diagram boundaries; tables stay intact               |
| Code repositories         | AST-based — functions, classes, modules as natural chunks      |
| Schemas / DDL             | Table-based — one chunk per table, comments included           |
| Slide decks (.pptx)       | One chunk per slide + speaker notes; visuals captioned by VLM   |
| Tabular reports (.xlsx)   | Sheet-based; large sheets row-batched with column headers as context |
| Long-form prose (.md/.docx) | Recursive splitter at 800–1200 tokens, with 100-token overlap |
| Transcripts / call notes  | Speaker-turn-based, with conversation segments                 |

Every chunk carries metadata that survives downstream:

```
chunk_id:        uuid
parent_doc_id:   reference to source document card
heading_path:    ["Section 4", "4.2 Training Compliance", "4.2.1 Cybersecurity"]
prev_chunk_id:   for boundary lookups during NER
next_chunk_id:   for boundary lookups during NER
token_count:     for cost estimation
content_hash:    for incremental skip
```

The heading path is what lets a claim like "must be completed within 30
days" be cited correctly later — the system knows the claim came from
Section 4.2.1 of the 2026 training policy, not from a generic blob.

Overlap between chunks is small (50–100 tokens at boundaries) and exists
only to preserve cross-references — a pronoun in chunk N+1 referring to an
entity introduced in chunk N. NER runs per-chunk but can stitch entities
across boundaries using the prev/next pointers when entity coreference
matters.

For very long documents (>50 chunks), a hierarchical summary tree is built
alongside the leaf chunks: each section gets a one-paragraph summary; each
chapter gets a one-paragraph summary of summaries. Retrieval can then start
at the summary tier and drill down only when needed.

### 11.2 Findings sizing (Stage 2)

Analysis findings can be massive — a correlation matrix across 500 columns
is 250,000 pairs, a causal DAG over a tenant's full schema can run to
thousands of edges. None of that should land as one finding object.

| Finding type              | Storage strategy                                           |
| ------------------------- | ---------------------------------------------------------- |
| Correlation matrix        | One finding per *significant* pair (above effect-size and p-value thresholds); the rest discarded |
| Causal DAG                | One finding per candidate edge; the graph reconstructs at query time |
| Outcome batches           | Window-bounded — per-month or per-cohort, not one giant batch |
| NER results               | One finding per typed-entity span, indexed by chunk        |
| Granger / time-series     | One finding per (variable_pair, lag), filtered to significant lags |

Significance thresholds are configurable per finding type and stored as
config cards in the Kinetic Layer (so they can themselves be reasoned
about). The discipline is: findings are atomic and cite-able; if a finding
is too large to read, it is too large to be useful, and it should be split.

### 11.3 Card neighborhood context (Stage 4 input)

When generating or updating a card, the LangGraph workflow loads context
from neighbors. A heavily-connected card — say a top-level `causal_node`
that participates in 30 edges — would blow the context window if every
neighbor was loaded at full body.

Tiered retrieval handles this:

| Tier   | What's loaded                                  | When used                                   |
| ------ | ---------------------------------------------- | ------------------------------------------- |
| Tier 1 | Full card body                                 | Direct neighbors (ref distance 1) — top 3–5 most relevant by embedding similarity to the change |
| Tier 2 | Card summary (first sentence + header)         | Next ring — refs of refs, cap ~10           |
| Tier 3 | Card ID + kind only                            | Beyond ring 2, used to flag "this exists" without loading |

The "most relevant by embedding similarity" criterion matters: a causal
edge update shouldn't have to read every neighbor of its source node, just
the ones whose embeddings are similar to the change being made. This is
the same retrieval pattern KnowQL uses, applied internally.

Every card carries a one-sentence summary in its header (auto-generated
on publish) precisely so Tier 2 retrieval is cheap. The summary is also
what Section 5.6 entity linking compares against during NER pass on draft.

### 11.4 Card body sizing (Stage 4 output)

Cards target 100–400 words. The LLM is prompted with that target and
warned at 600. When the topic genuinely needs more space — a causal edge
with a rich identification narrative, an object type with many implemented
interfaces — the generator splits rather than overflowing.

The split strategy is per card kind, because the natural axes of
decomposition differ:

| Card kind        | Split strategy when oversized                                          |
| ---------------- | ---------------------------------------------------------------------- |
| `object_type`    | Split by interface implementation: parent card has core definition + structure; child cards (`employee__as_trainable`, `employee__as_auditable`) carry interface-specific behavior |
| `link_type`      | Rare. If oversized, usually means two link types are conflated — split into separate links with distinct `derivation` or cardinality |
| `property_type`  | Split by aspect: `progress_percent` (semantics) + `progress_percent__distribution` (stats) + `progress_percent__derivation` (how it's computed) |
| `concept`        | Split into parent/child concepts using the L1/L2/L3 hierarchy already in use — abstract concept at L1, specializations at L2, instance-level at L3 |
| `causal_node`    | Parent node card + `<node>__evidence` card carrying accumulated observations + `<node>__priors` card if prior derivation needs detail |
| `causal_edge`    | Parent edge card with weight + effect summary; `<edge>__identification` card with the identifiability narrative; `<edge>__weight_history` card with evidence batches and refits over time |
| `derivation_rule` | Rule card stays terse; rationale and edge cases move to a `<rule>__rationale` companion card |
| `validation_rule` | Same pattern: rule terse, rationale separate                          |
| `causal_rule`    | Activation logic in primary card; contribution math (Shapley details) in a companion `<rule>__contribution` card |
| `action_type`    | Action card carries signature + preconditions + audit; `<action>__effects` card lists downstream effects when numerous |
| `function`       | Rare. Splits indicate the function should be decomposed into smaller functions — push that signal back upstream |
| `marking`        | Marking card stays terse. Propagation rules can move to a `<marking>__propagation` card if they grow complex |
| `role`           | Role card stays terse. If long, indicates the role bundles too many permissions — split the role |
| `permission`     | Almost always short. Long permission cards usually mean the permission is over-broad — split it |
| `audit_entry`    | Always short by design. If long, you are conflating multiple audit events |
| `lineage_edge`   | Always short by design. If long, you are conflating multiple lineage events |

A few of these are worth highlighting because they recur:

- **Causal edges split into three.** This is the most common oversize. The
  edge card carries the effect, weight, CI; the identification card
  carries the do-calculus story and confounder reasoning; the weight
  history card carries evidence batches and refit timeline. All three
  ref each other. A causal-effect query retrieves the edge card; a
  reviewer asking "why is this identifiable" retrieves the identification
  card; the Weight Learner reads the history card.

- **Object types split by interface.** When `Employee` carries behavior
  for Trainable, Auditable, Markable, and Governable interfaces, the
  parent card stays focused on the entity itself, and each interface
  implementation gets its own card. This keeps the parent readable and
  makes interface-scoped queries cheaper.

- **Concepts split into hierarchies.** This already aligns with the
  L1/L2/L3 vector-store pattern in use — abstract at L1, specializations
  at L2, instance-level at L3. Oversized concept cards are usually a
  signal that the L1/L2 split was missed.

### 11.5 How splits compose at retrieval time

Splitting cards has to be retrieval-transparent or it defeats the
readability principle. Two mechanisms make it work:

**Parent-child convention in card IDs.** A child card has the form
`<parent_id>__<aspect>`. Retrieval can find all children of a parent with
a prefix query in constant time. The reverse-reference index materializes
the relationship explicitly.

**Composite retrieval at query time.** When KnowQL retrieves a card, it
checks for children and either inlines their summaries (if total length
stays under a threshold) or returns them as related cards in the
response. From the user's perspective, asking about `employee` returns a
coherent answer regardless of whether the underlying ontology has one
card or six.

**Split detection during HITL review.** When the generator decides to
split a card, the split is itself a HITL-routed change — a reviewer
confirms the proposed split before publication. This prevents the LLM
from over-decomposing and creating a thicket of micro-cards. The default
threshold for proposing a split is 600 words; below that, the generator
tries to compress instead.

### 11.6 What this looks like in practice

A worked example: the policy document arrives at 80 pages. Stage 1
chunks it into 47 section-based chunks averaging 800 tokens each, with
heading paths preserved. Stage 2 NER finds 312 typed entity spans across
the chunks; entity linking resolves 287 to existing cards and queues 25
as new-entity candidates. Stage 2 claim extraction grounds on the linked
entities and produces 89 candidate claims, each tagged with its source
chunk and heading path.

Stage 4 generates updates to 31 existing cards and 8 candidate new cards
from those claims. Of the 31 updates, 4 cards exceed 600 words after
draft and are flagged for split — the generator proposes the splits, the
HITL queue catches them. Of the 8 candidate new cards, 6 auto-publish
after entity linking confirms no near-duplicates exist; 2 route to HITL
for new-concept review.

The 80-page document never appears whole anywhere in the pipeline after
Stage 1. It exists as 47 chunks for NER, 89 claims for generation, and
ultimately as 39 card touches (31 updates + 8 new). Each transformation
preserves provenance back to its chunk and heading path, so a reviewer
asking "where did this claim come from" gets a citation in two clicks.

---

## 12. Observability

The pipeline is only useful if its behavior is legible. Five views:

| View                  | What it shows                                           |
| --------------------- | ------------------------------------------------------- |
| **Run dashboard**     | Per-run: duration per stage, cards touched, cost, errors |
| **Card history**      | Per-card: version timeline with diffs and rationales     |
| **Causal edge tracker** | Weight evolution, CI narrowing, hypothesized→learned promotions |
| **HITL queue depth**  | Pending reviews by kind, age, priority                   |
| **Source coverage**   | Which sources fed which cards last run, age of stale ones |

### Tools

| Need                  | Recommendation                                         |
| --------------------- | ------------------------------------------------------ |
| Pipeline orchestration & monitoring | Dagster (cleanest for typed assets) or Airflow |
| LLM call tracing      | Langfuse or LangSmith                                  |
| Data lineage          | OpenLineage emitted from acquisition stage             |
| Metrics               | Prometheus + Grafana, or Datadog                       |
| HITL UI               | Custom — table-style review queue with diff view       |

---

## 13. Rollback

Every run is reversible at the card-version level. Two failure modes to plan for:

1. **Bad single card.** A card was published but contains an error.
   Rollback: promote the prior version of that card to "live" and mark the
   bad version as withdrawn. One-line operation.
2. **Bad full run.** A pipeline change introduced a systemic flaw and
   tonight's run produced bad cards across the board. Rollback: every
   card touched by the run is reverted to its prior version atomically.
   The run manifest is marked "withdrawn" but retained for forensics.

Both modes write audit entries describing the rollback so the history is
complete.

---

## 14. Cost Considerations

LLM calls dominate. Four levers:

1. **Don't regenerate unchanged cards.** Stage 3 is the most important cost
   control. Caching by content hash means a typical day touches single-digit
   percentages of the corpus.
2. **Cheap models for cheap cards.** Routine `object_type` and
   `property_type` updates use a fast/small model. `causal_edge` and
   `concept` work uses the strongest model available.
3. **Batch where possible.** Same-kind generations share a system prompt and
   run in a single batch call where the LLM provider supports it.
4. **Let NER do the cheap work.** Entity enumeration, span detection, and
   reference resolution are cheap when handled by NER (CPU-bound, no API
   call) and expensive when delegated to the LLM. Pushing those tasks to
   NER lets the LLM focus on prose generation, which it does well, and cuts
   token usage on the generation step by 30–50% in typical runs.

Rough budget per run for a 5,000-card corpus with typical change rates: $5–15
in LLM costs, dominated by document claim extraction (which scales with
document volume, not corpus size). First-run bootstrap is materially more
expensive — typically $200–800 depending on corpus size.

---

## 15. Open Design Questions

1. **Per-tenant vs shared pipeline.** Multi-tenant deployment: do tenants
   share a single pipeline run schedule and segregate by namespace, or run
   independent pipelines per tenant? Independent is cleaner for blast radius;
   shared is cheaper. Probably independent at scale, shared at start.

2. **Real-time updates for high-priority changes.** Daily is fine for most
   cards. Some inputs (a new permission, a critical document change) might
   warrant a faster path. A "high-priority lane" that bypasses the daily
   schedule and runs on-event is feasible but adds complexity.

3. **Document claim provenance granularity.** Per-document, per-section, or
   per-sentence? Per-section is the working answer — fine enough to cite,
   coarse enough to keep the index small.

4. **Causal structure discovery — which algorithm wins?** Run multiple,
   intersect for high agreement, surface disagreements. But which serves as
   primary? PC for default, FGES when graphs grow large. Worth A/B-ing on
   real data before committing.

5. **HITL reviewer tooling.** Build a custom review UI vs. point reviewers at
   git-style diffs. Custom UI is more usable; git-style is faster to ship and
   integrates with existing review habits. Probably git-style first, custom
   UI when reviewer volume justifies it.

6. **Outcome data labeling latency.** Causal weight learning needs ground
   truth. Some outcomes (did the assignment go overdue) are observable in
   days; others (did training reduce phishing susceptibility) take months.
   The Weight Learner needs to handle multiple latency horizons cleanly,
   probably with separate refit cadences per causal edge.

7. **Code repo claim extraction.** Static analysis of validation logic in
   code is high-value but high-effort to do well across languages. Start
   with one stack (probably Python + SQL + dbt) and expand.

8. **NER strategy — zero-shot vs fine-tuned.** GLiNER's zero-shot regime is
   the right starting point — no labeled data, type set evolves freely. As
   the corpus grows, fine-tuning a domain encoder (BERT or Flair) on
   accumulated NER outputs becomes cheaper per call and more accurate. The
   open question is when to switch: probably once we have ~10k labeled
   spans across the eight or so type classes, and once the type set has
   stabilized. A gradient handoff (fine-tuned for stable types, zero-shot
   for new types) is also feasible.

9. **Entity linking thresholds per kind.** Object types tolerate a tight
   threshold because their canonical names are stable. Concepts need looser
   matching because the same idea surfaces in many phrasings. Causal markers
   need rule-based matching, not embedding similarity, because the lexicon
   is small and stable. Per-kind thresholds need to be tuned empirically
   against the false-positive and false-negative rates we observe in HITL
   reviews — too tight and the new-entity queue floods; too loose and
   distinct concepts get merged.

---

## 16. What Ships First

A staged delivery plan to make this real:

**Phase 1 — Bootstrap.** Acquisition + Analysis + Card Generation for
`object_type`, `link_type`, `property_type`. Schemas and profiling only. No
documents, no causal edges yet. End: a navigable but flat ontology.

**Phase 2 — Documents.** Add document loading and claim extraction. Add
`concept` and `derivation_rule` card kinds. End: ontology with domain meaning.

**Phase 3 — Causal.** Add causal structure discovery, `causal_node`,
`causal_edge` generation as hypothesized. Add `causal_rule`. End: ontology
with causal hypotheses but no learned weights yet.

**Phase 4 — Outcomes and Learning.** Add outcome data acquisition, Weight
Learner, hypothesized→learned promotion. End: causal claims with evidence.

**Phase 5 — Governance.** Add `marking`, `permission`, `role` cards from
policy docs and existing access control. Add HITL routing. End: production-
governable ontology.

**Phase 6 — Tuning loop.** Add user-correction signals, query-log feedback,
active learning prompts. End: self-improving ontology.

Each phase is end-to-end shippable on its own and produces useful cards. No
phase gates the next; you can run Phase 1 in production while Phase 2 is in
development.
