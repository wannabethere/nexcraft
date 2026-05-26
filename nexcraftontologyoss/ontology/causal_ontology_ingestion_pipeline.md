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
generation consumes. Three sub-stages, each independent and parallelizable.

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

### 5.4 Claim extraction from documents

Each document chunk is passed through a structured-output LLM call that
extracts:

- **Definitions**: "An active employee is one whose status is not terminated
  and whose end_date is null." → candidate property semantics.
- **Rules**: "Mandatory cybersecurity training must be completed within 30
  days of hire." → candidate `derivation_rule` or `validation_rule`.
- **Causal claims**: "Phishing simulation training reduces successful phishing
  attempts by ~40% in our environment." → candidate `causal_edge`.
- **Governance**: "Only HR Compliance Officers may modify training due dates."
  → candidate `permission`.

Each extraction carries provenance back to the document chunk. The Card
Generation stage ranks claims by source authority — a policy document beats
a training slide deck.

### 5.5 Tools for analysis

| Need                          | Recommendation                                       |
| ----------------------------- | ---------------------------------------------------- |
| Correlation suite             | `scipy.stats`, `phik`, `dython`                      |
| Causal structure              | `causal-learn`, `dowhy`, `lingam`                    |
| Time-series causality         | `statsmodels`, `tigramite`                           |
| Claim extraction              | LLM with Pydantic structured output (your existing pattern) |
| NER (typed entities)          | `spaCy`, `GLiNER` for zero-shot typed extraction     |
| Document chunking             | `LlamaIndex` node parsers, `LangChain` text splitters |

### 5.6 Output contract

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

A LangGraph workflow per card kind, all following the same shape:

```
            ┌──────────────┐
            │ Load context │   prior version + neighbors + relevant findings
            └──────┬───────┘
                   ▼
            ┌──────────────┐
            │  Plan edits  │   what needs to change in this card and why
            └──────┬───────┘
                   ▼
            ┌──────────────┐
            │ Draft prose  │   regenerate body with updated facts
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

## 11. Observability

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

## 12. Rollback

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

## 13. Cost Considerations

LLM calls dominate. Three levers:

1. **Don't regenerate unchanged cards.** Stage 3 is the most important cost
   control. Caching by content hash means a typical day touches single-digit
   percentages of the corpus.
2. **Cheap models for cheap cards.** Routine `object_type` and
   `property_type` updates use a fast/small model. `causal_edge` and
   `concept` work uses the strongest model available.
3. **Batch where possible.** Same-kind generations share a system prompt and
   run in a single batch call where the LLM provider supports it.

Rough budget per run for a 5,000-card corpus with typical change rates: $5–15
in LLM costs, dominated by document claim extraction (which scales with
document volume, not corpus size). First-run bootstrap is materially more
expensive — typically $200–800 depending on corpus size.

---

## 14. Open Design Questions

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

---

## 15. What Ships First

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
