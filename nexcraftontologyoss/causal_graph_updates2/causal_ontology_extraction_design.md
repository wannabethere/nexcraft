# Knowledge Extraction — Constructs, Models, Tools, and Execution

The extraction layer that sits between raw data sources and the card-generation
pipeline. Defines the constructs that turn data into structured findings, the
models and tools that power them, and the execution backends that run them at
scale. Ships in two primary modes — Spark for warehouse-co-located deployments,
standalone for everything else — with Ray as a pluggable extension for feature
engineering and data flow.

---

## 1. Purpose and Scope

The Knowledge Engine pipeline (see `causal_ontology_ingestion_pipeline.md`)
consumes structured findings — correlations, causal structure candidates, NER
spans, document claims, profiling stats — and produces card edits. It does not
care where the findings came from, only that they conform to the finding
contract.

This document specifies what produces those findings: the **extractors**,
**profilers**, **analyzers**, **NER pipelines**, **claim extractors**, **entity
linkers**, and **feature engineers** that turn raw source data into the
findings the card generators need. It also specifies the **execution
backends** that run them — Spark, standalone, and Ray — and the abstractions
that let the same extraction logic run on any of them.

The scope:

- The conceptual building blocks (constructs) of extraction.
- The models — statistical, ML, NER, LLM, causal — that constructs use.
- The tools (libraries, services) that implement the models.
- The operator abstraction that decouples constructs from execution backends.
- The three backends and their tradeoffs.
- Resource management, state, observability, and failure handling.

Out of scope: the card store, the graph maintainer, the eval framework, the
KnowQL planner. Those are downstream consumers of findings, covered in their
own design docs.

---

## 2. Architecture Overview

Extraction is organized as a directed acyclic graph of **operators**. Each
operator consumes typed artifacts (raw data, prior findings, intermediate
state) and produces typed artifacts (more findings, or downstream-ready
inputs). Operators are pure functions of their inputs in the contract sense —
given the same inputs and configuration, they produce the same outputs.

The execution backend is decoupled from the operator definition. The same
operator can run as a Spark job, as a Python in-process call, or as a Ray task.
The choice of backend depends on the deployment context (customer's
infrastructure, data scale, latency requirements) and on the operator's
characteristics (some operators are SQL-heavy and belong on Spark; some are
ML-heavy and belong on Ray).

```
┌──────────────────────────────────────────────────────────────────────┐
│  CONSTRUCTS (engine-agnostic operator definitions)                    │
│                                                                        │
│  Source Adapters │ Profilers │ Correlators │ Causal Discovery          │
│  Document Chunkers │ NER Pipelines │ Claim Extractors                  │
│  Entity Linkers │ Feature Engineers │ Outcome Collectors               │
└──────────────────────────────────────────────────────────────────────┘
                              │
                              ▼
┌──────────────────────────────────────────────────────────────────────┐
│  OPERATOR ABSTRACTION                                                  │
│  Operator interface │ Artifact contract │ Execution context           │
│  Resource declarations │ Idempotency keys                             │
└──────────────────────────────────────────────────────────────────────┘
                              │
              ┌───────────────┼───────────────┐
              ▼               ▼               ▼
┌──────────────────┐  ┌──────────────────┐  ┌──────────────────┐
│  SPARK BACKEND   │  │ STANDALONE BACKEND│  │   RAY BACKEND    │
│                  │  │                  │  │                  │
│  PySpark jobs    │  │ Pandas/Polars    │  │ Ray Tasks        │
│  Spark SQL       │  │ DuckDB           │  │ Ray Datasets     │
│  Delta Lake      │  │ Postgres+Qdrant  │  │ Ray Actors       │
│  Spark NLP       │  │ In-process Python│  │ Ray Train/Serve  │
└──────────────────┘  └──────────────────┘  └──────────────────┘
              │               │               │
              └───────────────┼───────────────┘
                              ▼
┌──────────────────────────────────────────────────────────────────────┐
│  ARTIFACTS (findings, intermediate state, source bindings)            │
│  Written to artifact store, consumed by card generation pipeline      │
└──────────────────────────────────────────────────────────────────────┘
```

The operator abstraction is the spine. Constructs are specified once;
backends adapt them. Customers running on Databricks get a Spark execution
plan; customers running on a single VM get the standalone backend; customers
who need distributed feature engineering or online inference can plug Ray
into either.

---

## 3. Constructs

Each construct is a family of operators sharing input/output contracts and
configuration patterns. The construct is the conceptual unit; specific
operator instances configure the construct for a particular source or task.

### 3.1 Source Adapters

Operators that pull raw data from a source system and normalize it into a
typed source artifact. One adapter per source kind.

| Adapter                  | Pulls                                              | Normalizes to                         |
| ------------------------ | -------------------------------------------------- | ------------------------------------- |
| `WarehouseSchemaAdapter` | DDL via information_schema / SHOW TABLES           | `SchemaArtifact` with tables, columns, types, FKs |
| `WarehouseDataAdapter`   | Sampled rows, full tables, query results           | `DataArtifact` with rows + schema     |
| `DbtManifestAdapter`     | dbt's manifest.json                                | `LineageArtifact` + `SchemaArtifact`  |
| `DocumentFolderAdapter`  | Files from filesystem, S3, Drive                   | `DocumentArtifact` per file           |
| `PdfAdapter`             | PDF files with layout                              | `DocumentArtifact` with chunks + tables + images |
| `SlideDeckAdapter`       | .pptx with slides + speaker notes                  | `DocumentArtifact` per slide           |
| `SpreadsheetAdapter`     | .xlsx/.csv with sheets                             | `DataArtifact` per sheet               |
| `CodeRepoAdapter`        | Git repo with language detection                   | `CodeArtifact` per file with AST       |
| `QueryLogAdapter`        | Warehouse query history                            | `QueryLogArtifact` with parsed SQL     |
| `OutcomeStreamAdapter`   | Application events with labels                     | `OutcomeArtifact` with windowed batches |
| `CatalogAdapter`         | Atlan, Collibra, AWS Glue, Unity Catalog metadata  | `SchemaArtifact` + `DocumentArtifact`  |
| `OntologyAdapter`        | External ontologies (CWE, MedDRA, SNOMED)          | `ConceptHierarchyArtifact`             |

Each adapter handles authentication, rate limiting, incremental fetches via
watermarks or content hashes, and normalization to the typed artifact. The
adapter does not interpret content — interpretation happens downstream. This
separation lets the same downstream constructs work on any source.

Adapters declare their **incremental contract**: how they identify what's new
since the last run. Three patterns are supported:

- **Hash-based**: hash the full content; rerun if hash changed (schemas,
  documents).
- **Watermark-based**: track a high-water mark (timestamp, ID); fetch only
  rows beyond it (outcome streams, query logs).
- **Time-windowed**: pull a rolling window (last 24 hours of profiling stats).

The adapter author chooses the pattern; the framework persists the watermark
state across runs.

### 3.2 Profilers

Operators that compute summary statistics on data artifacts. Three levels:

**Column profilers.** Per-column statistics: type, null rate, distinct count
(via HLL when exact is too expensive), top-k values, percentiles, histograms,
patterns (regex characterization), text length distribution for string columns.

**Table profilers.** Per-table statistics that span columns: row count,
duplicate rate, key uniqueness checks, conditional distributions (e.g., null
rate of column B given column A is set).

**Dataset profilers.** Multi-table statistics: cross-table cardinality
estimates for joins, FK validity rates (do all employee_ids in
training_assignment exist in employee?), referential integrity scores.

Profilers produce `ProfileArtifact`s with provenance back to the data they
profiled. They are the inputs both to card generation (a property_type card's
header pulls range and distribution from a column profile) and to downstream
analyzers (correlation, causal discovery).

The cost-control discipline for profilers is critical. A naive profile of
every column on every run is unaffordable on a billion-row table. The
framework supports:

- **Sample-based profiling** with stratification on key columns when full
  scans are too expensive.
- **Approximate algorithms** (HyperLogLog, t-digest, count-min sketch) for
  cardinality and quantile estimates.
- **Incremental profiling** that updates statistics from the last known
  state plus the delta.
- **Skip-when-unchanged** based on row-count and modification-timestamp deltas.

The right combination depends on the source; profilers are configurable.

### 3.3 Statistical Correlation Pipeline (Four Tiers)

Pairwise correlation across all columns of all tables is combinatorially
expensive at realistic enterprise scale (200 tables, 5000 columns ≈ 12M
pairs). Naive exhaustive testing is infeasible and produces an unmanageable
false-positive rate. The pipeline runs as **four tiers, each progressively
more expensive and more selective**, with the math layer entirely LLM-free.

This construct replaces a flat correlator with a tiered architecture. The
tiers correspond to four distinct operator families that flow into each
other, each writing findings the next tier reads.

#### Tier 1 — Pre-filter (no statistics, no LLM in math)

Pure metadata reasoning. Drops pair candidates that are structurally
implausible before any test runs. Reduces candidate pairs by 95-98%
typically.

| Filter                | Mechanism                                            | Drops                          |
| --------------------- | ---------------------------------------------------- | ------------------------------ |
| Schema-level          | Cross-CDM-entity plausibility from seed registry      | Pairs across unrelated entities |
| Type-level            | Type compatibility rules                              | Free-text vs numeric, ID vs ID  |
| Cardinality-level     | From profiler outputs                                 | >99% same, >95% null, >99% unique |
| Embedding-level       | Column-level embeddings (name + comment + top-k values) ranked by similarity | Pairs below 70th percentile |
| Seed-prior boost      | Pairs participating in seed causal priors prioritized | Doesn't drop; reorders          |

Tools: sqlglot for schema parsing, dbt manifest reader, sentence-transformers
for column embeddings, Qdrant for similarity ranking, custom rule sets for
type compatibility. **No LLM in the math.** A bounded LLM call per
tenant-custom column may attach a one-sentence semantic description used in
embedding similarity — its output is metadata, not statistics.

Output: `CandidatePairArtifact` listing pairs to test, with priority labels
from seed-aware reordering.

#### Tier 2 — Vectorized statistical screening

Cheap, vectorized statistical tests across all surviving candidate pairs.
The goal is surfacing pairs with non-trivial dependence, not estimating
effect sizes precisely. Multiple-testing correction is mandatory.

| Pair type                | Test                                              | Library              |
| ------------------------ | ------------------------------------------------- | -------------------- |
| Numeric ↔ Numeric        | Spearman (default), Pearson, distance correlation | `scipy.stats`, `dcor` |
| Categorical ↔ Categorical | Cramér's V, Theil's U                             | `dython`             |
| Mixed                    | phik (φK), correlation ratio η                    | `phik`, `dython`     |
| Any ↔ Any (catch-all)    | Mutual information                                | `sklearn.feature_selection` |
| Time-series              | Cross-correlation                                 | `statsmodels`        |

**Multiple-testing correction.** Benjamini-Hochberg FDR correction applied
within scope (within Spearman tests separately from within Cramér's V
tests). Tool: `statsmodels.stats.multitest.multipletests` with
`method='fdr_bh'`.

**Effect-size threshold alongside significance.** A statistically significant
correlation of 0.02 is real but uninteresting. Drop pairs with effect size
below a threshold scaled to sample size:
`threshold = max(0.1, 2/sqrt(n))`.

**Stratification when seed-known.** When seed knowledge declares a
meaningful stratification axis (e.g., `Role` for training-related pairs),
correlations are computed both unstratified and stratified. Simpson-paradox
risk is surfaced when stratified and unstratified results diverge meaningfully.

The Tier 2 operators produce `CorrelationFindingArtifact` per surviving
pair with: method, statistic, p-value (FDR-corrected), n, effect-size
flag, stratification result if applicable.

**Compute pattern.** Tier 2 is embarrassingly parallel — perfect fit for
Spark or Ray Datasets. Shard candidate pairs across executors; each
executor runs scipy/dython on its shard; results aggregate at the driver
for FDR correction. 500K candidate pairs on a 16-executor cluster
complete in minutes.

Output: 1K-10K flagged pairs from typical 100K-500K candidates.

#### Tier 3 — Targeted expensive analysis

For pairs that survived Tier 2, run analyses that produce findings
suitable for causal reasoning.

| Analysis                    | Purpose                                              | Library                  |
| --------------------------- | ---------------------------------------------------- | ------------------------ |
| Bootstrap CIs               | Stability of effect estimate                         | `scipy.stats.bootstrap`  |
| Conditional independence    | Bridge to causal: does conditioning on C kill A↔B?    | `causal-learn` cit module |
| Partial correlation         | Effect after conditioning on candidate confounders   | `pingouin`               |
| Granger causality           | Time-series predictive causality                     | `statsmodels`            |
| Cross-correlation with lags | Time-series with unknown delays                       | `statsmodels`            |
| Transfer entropy            | Information-theoretic causality, non-linear          | `PyCausality`, `IDTxl`   |
| Multivariate MI             | Multi-way conditioning                                | `npeet`                  |
| Refutation tests            | Sanity check on flagged correlations                 | `dowhy`                  |

**Where context (and bounded LLM use) enters.** Conditional independence
tests need a candidate confounder set. Seed knowledge supplies most:
pack causal priors declare confounders for known edges. For non-seed
pairs, a bounded LLM call may *propose* additional candidate confounders
(its output: list of column names from the schema). The conditional
independence test then runs and validates each candidate. **The LLM
proposes; the math validates.** A confounder candidate that doesn't
materially change the partial correlation is rejected.

**Refutation tests.** For high-stakes correlations (those involved in
governance-critical edges, or above an importance threshold), run
DoWhy refutation tests: placebo treatment, random common cause, data
subset stability. Failed refutations demote the finding regardless of
the original effect size.

Tier 3 produces `ValidatedCorrelationArtifact` per pair with:
all Tier 2 fields plus bootstrap CI, conditional independence results
across confounder sets, refutation outcomes.

**Compute pattern.** Per-pair expensive computation — natural fit for
Ray Tasks (one task per pair, GPU access for tasks that benefit).
On Spark, runs as a UDF over the surviving-pairs DataFrame.

Output: 100-500 high-quality correlation findings.

#### Tier 4 — Causal Structure Discovery

Causal discovery on the variables that survived as participants in Tier 3
findings. **Discovery does not run on the full schema** — it runs on the
50-200 variables Tier 3 surfaced. This is what makes discovery tractable;
PC on 5000 variables is computationally infeasible, PC on 200 is fine.

| Algorithm   | When                                           | Library         |
| ----------- | ---------------------------------------------- | --------------- |
| PC          | Default; observational data, no time order     | `causal-learn`  |
| FGES        | Larger graphs; score-based, scales better      | `causal-learn`, `py-causal` |
| GES         | Greedy equivalence search; Gaussian linear     | `causal-learn`  |
| LiNGAM      | Non-Gaussian noise assumption holds            | `lingam`        |
| DirectLiNGAM | Faster LiNGAM variant                         | `lingam`        |
| NOTEARS     | Continuous, differentiable                     | `notears`       |
| PCMCI       | Time-series, multivariate, with confounders    | `tigramite`     |

The discovery construct runs **multiple algorithms in parallel** and emits
each algorithm's edges separately. A downstream `CausalConsensus` operator
intersects them: edges in 3+ algorithms with consistent direction are
flagged "high agreement"; disagreement edges are flagged "needs review."
The Causal Hypothesizer in card generation reads both — high-agreement
edges become hypothesized `causal_edge` cards with elevated initial
confidence; disagreement edges become hypothesized cards with lowered
confidence and a prose note about the algorithmic disagreement.

Discovery output is **direct edges only** — no transitive closure, no
multi-hop paths. Multi-hop reasoning is computed at query time from the
direct edge corpus, bounded by the depth-3 default in KnowQL.

**Compute pattern.** Graph-shaped, not naturally parallelizable across
edges. Best run on a single executor with adequate memory (FGES needs
RAM for score caching). Spark uses one executor; Ray uses an actor;
standalone runs in-process.

#### When data is not available

The four-tier pipeline assumes sample data is accessible. When it isn't
(Mode C from the configuration doc), the pipeline degrades cleanly:

| Tier   | Mode A: Warehouse | Mode B: API+Data | Mode C: Metadata Only | Mode D: Documents Only |
| ------ | ----------------- | ---------------- | --------------------- | ---------------------- |
| Tier 1 | Full              | Full             | Full (uses metadata)  | N/A — no schema        |
| Tier 2 | Full              | Sample-bounded   | Skip — emit "stats unavailable" | Skip      |
| Tier 3 | Full              | Sample-bounded   | Skip                  | Skip                   |
| Tier 4 | Full              | Sample-bounded; flag findings as "from sample" | Use seed causal priors only | Use seed causal priors only |

In Mode C, the pipeline still produces causal findings — but exclusively
from seed causal priors matched against the tenant's available concepts.
The Causal Prior Matcher (Foundry §4.5) runs in any mode; the discovery
suite skips. This is what gives metadata-only deployments a working
causal foundation from day one without statistical evidence: literature-
backed hypothesized edges keyed to seed concepts.

In Mode B with limited samples, Tier 4 algorithms tolerant of small data
(PC, LiNGAM) run; sample-size-sensitive algorithms (FGES, NOTEARS) skip.
Findings are flagged `weight.source: hypothesized_from_sample` rather
than `learned`. The Weight Learner downstream may promote them to
`learned` after enough outcome data accumulates.

The discipline at every tier: **operators emit explicit "unavailable"
artifacts when they can't run, never silently produce empty findings.**
Downstream consumers see the unavailable artifact and adjust their
behavior; the absence of a finding is itself information.

#### Cross-cutting: LLM-free math

Across all four tiers, the math is LLM-free. LLMs participate at the
edges only:

- **Tier 1**: optional bounded LLM call per tenant-custom column to
  attach semantic description for embedding similarity.
- **Tier 3**: optional bounded LLM call to propose candidate confounders
  for non-seed pairs; the conditional independence test validates each.
- **Post-statistics**: LLM frames computed findings in prose for card
  bodies; the eval framework's quantitative integrity check ensures
  prose stays consistent with header values.

What LLMs never do: compute correlation values, decide significance,
estimate effect sizes, run causal discovery algorithms, validate
identifiability, compute Shapley attributions. These are mechanical
operations on data; their outputs are reproducible and auditable.

### 3.4 Document Chunkers

Operators that split documents into typed chunks suitable for downstream NER
and claim extraction. Strategy depends on document type, as detailed in
§11.1 of the ingestion plan.

| Chunker                   | For document type                                       |
| ------------------------- | ------------------------------------------------------- |
| `MarkdownHeaderChunker`   | Markdown, including converted Word docs                 |
| `PdfStructuralChunker`    | PDFs with detected structure (headings, sections)       |
| `PdfFallbackChunker`      | PDFs with no structure — recursive token-based splitting |
| `SlideDeckChunker`        | One chunk per slide + speaker notes                     |
| `CodeAstChunker`          | Functions, classes, modules as chunks                   |
| `SchemaTableChunker`      | One chunk per table with all columns and comments       |
| `SpreadsheetChunker`      | Sheet-based with row batching for large sheets          |
| `TranscriptChunker`       | Speaker-turn-based                                       |
| `RecursiveTextChunker`    | Generic fallback — recursive 800–1200 token splitting    |

Every chunker emits chunks with the metadata header from §11.1: `chunk_id`,
`parent_doc_id`, `heading_path`, `prev_chunk_id`, `next_chunk_id`,
`token_count`, `content_hash`. Heading paths and adjacency pointers are what
let downstream NER and claim extraction stitch entities and reference back to
specific document locations.

For long documents (>50 chunks), chunkers also emit a hierarchical summary
tree alongside the leaf chunks. This is generated by an LLM call per
section/chapter; it lets retrieval start at the summary tier and drill down.

### 3.5 NER Pipelines

Operators that detect typed entity spans in text. Hybrid implementation as
specified in §5.4 of the ingestion plan: GLiNER for domain-typed zero-shot,
spaCy for high-accuracy generic types, rule-based for stable lexicons (causal
markers).

The NER pipeline is itself a small DAG of operators:

```
        Document chunks
              │
              ▼
        ┌──────────────┐
        │  spaCy NER   │   generic types (PERSON, ORG, DATE, MONEY, ...)
        └──────┬───────┘
               │
               ▼
        ┌──────────────┐
        │  GLiNER NER  │   domain-typed zero-shot (entity_name, attribute,
        │              │   concept, event, actor_role, policy_reference,
        │              │   quantitative_claim, temporal_qualifier)
        └──────┬───────┘
               │
               ▼
        ┌──────────────┐
        │  Rule-based  │   causal_marker lexicon (reduces, increases,
        │  scanner     │   leads to, prevents, mitigates, ...)
        └──────┬───────┘
               │
               ▼
        ┌──────────────┐
        │   Merge &    │   resolve overlapping spans, prefer higher-confidence
        │   normalize  │   types, attach character offsets and chunk pointers
        └──────────────┘
```

Each stage is an operator. They can run sequentially in standalone mode, in
parallel partitions in Spark, or as concurrent Ray tasks. The merge step
handles the common case where spaCy and GLiNER find overlapping spans —
rules favor the more specific type and the higher confidence.

NER output is `EntitySpanArtifact` per chunk: a list of typed spans with
character offsets, surface forms, confidence scores, and the model that
detected them. Spans are the input to entity linking and the grounding
constraint for claim extraction.

### 3.6 Claim Extractors

LLM-based operators that pull structured claims from document chunks,
grounded on the entity spans that NER found. Four claim types per the
ingestion plan: definitions, rules, causal claims, governance.

The construct is a single LangGraph workflow per claim type:

```
        Chunk + entity spans
              │
              ▼
        ┌──────────────┐
        │ Build prompt │   chunk text + entity list +
        │              │   "claims must reference at least one entity"
        └──────┬───────┘
               │
               ▼
        ┌──────────────┐
        │  LLM call    │   structured output via Pydantic schema
        └──────┬───────┘
               │
               ▼
        ┌──────────────┐
        │  Validate    │   every claim references real entities;
        │              │   spans align with chunk; no fabrication
        └──────┬───────┘
               │
        validation passes? ─── no ──► retry with feedback (max 3)
               │ yes
               ▼
        ┌──────────────┐
        │ Emit findings │  one finding per claim with provenance
        └──────────────┘
```

Per claim type, the LLM prompt and Pydantic schema differ. The validation
rules differ — causal claims must reference at least two entities (source and
target); definitions must reference at least one attribute; governance claims
must reference at least one actor_role. The retry logic is the same.

Cost-wise, claim extraction dominates the LLM bill. Three controls:

- **Chunk skip** when chunk content_hash hasn't changed since last extraction.
- **Sample-then-cover** for very long documents: extract from the first N
  chunks, identify the claim density, decide whether to process the rest.
- **Tiered models**: cheap model for low-stakes documents (training slides),
  strong model for high-stakes (compliance policies, FDA submissions).

### 3.7 Entity Linkers

Operators that map NER spans to existing cards, queue new entity candidates,
and normalize variant surface forms. Per §5.6 of the ingestion plan.

The linker is a three-stage pipeline:

```
        Entity span
              │
              ▼
        ┌──────────────┐
        │ Exact match  │   look up canonical form against reverse-ref index
        └──────┬───────┘
               │ no match
               ▼
        ┌──────────────┐
        │ Embedding    │   embed span + sentence; query Qdrant for top-3
        │ similarity   │   above per-kind threshold
        └──────┬───────┘
               │ no match
               ▼
        ┌──────────────┐
        │ Queue as new │   route to HITL for new-entity review
        │ candidate    │
        └──────────────┘
```

Per-kind similarity thresholds are tuned empirically and stored as
configuration cards. Object types tolerate tight thresholds (canonical names
stable); concepts need looser thresholds (many phrasings); causal markers use
rule matching, not embedding. Cross-document deduplication clusters similar
spans before linking to reduce HITL queue volume.

### 3.8 Feature Engineers

The newest construct in the design and the one that justifies Ray. Feature
engineers transform raw artifacts into derived features that go into models —
either the system's own causal weight learner or downstream customer ML.

| Feature engineer            | Produces                                                |
| --------------------------- | ------------------------------------------------------- |
| `WindowAggregator`          | Rolling counts/sums/averages over time windows           |
| `CategoricalEncoder`        | One-hot, target, frequency, embedding-based encodings   |
| `TemporalDeriver`           | Day-of-week, time-since-event, seasonality features     |
| `JoinFeaturizer`            | Features from joining across entities (employee features × department features) |
| `EmbeddingFeaturizer`       | Pretrained or fine-tuned embeddings of text/categorical inputs |
| `GraphFeaturizer`           | Node centrality, community membership, motif counts on the semantic or causal graph |
| `CausalFeaturizer`          | Predicted treatment-effect estimates as features, propensity scores |
| `OutcomeLabeler`            | Joins outcome streams with feature vectors at the right time horizon |

These are the operators that benefit most from Ray's distributed execution
model. A `WindowAggregator` over a 12-month window across a million
employees is embarrassingly parallel — split by employee, compute
independently, materialize. A `GraphFeaturizer` computing centrality on a
multi-million-edge graph fits Ray's actor model. The standalone backend can
run these for small data; Spark works for SQL-shaped feature engineering;
Ray is the backend that scales the harder feature shapes cleanly.

The framework supports **feature pipelines** — composable feature engineer
chains with explicit feature definitions, lineage from raw artifacts, and
versioning. This is the closest the design comes to a feature store. Worth
building well because the same feature definitions feed the Weight Learner,
downstream causal effect estimation, and customer-side model training.

### 3.9 Outcome Collectors

Operators that pull labeled outcome data — did the predicted overdue actually
happen, did the intervention reduce the gap — and join it with the features
that were available at prediction time. The output is the training data for
the Weight Learner.

Three time horizons need to be handled cleanly:

- **Fast outcomes** (days): assignment overdue or not, reminder click-through.
- **Medium outcomes** (weeks to months): course completion, compliance
  attestation, training effectiveness measured by post-test.
- **Slow outcomes** (months to years): phishing incident reduction,
  certification expiration impact, organizational compliance posture.

Each horizon has a separate `OutcomeArtifact` with its own watermark. The
Weight Learner reads them all but refits causal edges on different cadences
based on outcome horizon — fast-outcome edges refit weekly, slow-outcome
edges refit quarterly.

---

## 4. Models

The models — statistical, ML, NER, LLM, causal — that constructs use. This
section catalogs them, says when each is used, and notes implementation
quirks worth knowing before building.

### 4.1 Statistical models

| Model                      | Purpose                                | Library              |
| -------------------------- | -------------------------------------- | -------------------- |
| Pearson correlation        | Linear numeric ↔ numeric                | `scipy.stats.pearsonr` |
| Spearman correlation       | Monotonic numeric ↔ numeric             | `scipy.stats.spearmanr` |
| Kendall's tau              | Robust monotonic                        | `scipy.stats.kendalltau` |
| Cramér's V                 | Categorical ↔ categorical                | `scipy.stats.contingency` |
| Theil's U                  | Asymmetric categorical association      | `dython`             |
| phi-K (φK)                 | Mixed-type, captures non-linear          | `phik`               |
| Mutual information         | Non-parametric, any types               | `sklearn.feature_selection` |
| Distance correlation       | Non-linear, rotation-invariant          | `dcor`               |
| Granger causality test     | Time-series predictive causality        | `statsmodels`        |
| Augmented Dickey-Fuller    | Stationarity check (Granger preqresite) | `statsmodels`        |
| Bootstrap CI               | Confidence intervals on any statistic    | `scipy.stats.bootstrap` |

These run inside correlators and in the Weight Learner's CI estimation. They
are CPU-bound and parallelize trivially across pairs.

### 4.2 Causal discovery models

| Algorithm   | Strengths                                 | Weaknesses                              | Library         |
| ----------- | ----------------------------------------- | --------------------------------------- | --------------- |
| PC          | Sound under faithfulness; widely used      | O(n²) edges to test; sensitive to tests | `causal-learn`  |
| FGES        | Score-based; scales to thousands of variables | Assumes Gaussian linear by default     | `causal-learn`, `py-causal` |
| GES         | Cleaner Gaussian results                   | Linear-Gaussian only                     | `causal-learn`  |
| LiNGAM      | Identifies direction with non-Gaussian noise | Requires non-Gaussianity assumption    | `lingam`        |
| DirectLiNGAM | Faster, more robust LiNGAM                 | Same assumptions                         | `lingam`        |
| NOTEARS     | Differentiable, GPU-friendly                | Linear by default; nonlinear extensions slower | `notears`  |
| GES + GIES  | Handles interventions if data has them     | Requires interventional flagging         | `causal-learn`  |
| PCMCI       | Time-series with confounders               | More complex setup                       | `tigramite`     |

Run multiple, intersect outputs, surface consensus. None is right for all
cases; the consensus operator is what produces actionable findings.

### 4.3 NER models

| Model              | Type                                  | Tradeoffs                                   |
| ------------------ | ------------------------------------- | ------------------------------------------- |
| spaCy `en_core_web_trf` | Pretrained transformer pipeline   | Strong on generic types, slow without GPU   |
| spaCy `en_core_web_lg`  | Pretrained CNN pipeline           | Faster, slightly less accurate              |
| GLiNER             | Zero-shot domain-typed                 | Excellent for evolving type sets, no training |
| Flair NER          | Bidirectional LSTM/transformer NER     | Good when fine-tuning data accumulates      |
| BERT/RoBERTa fine-tuned | Domain-specific NER              | Best accuracy when labeled data is available |
| Stanza             | High-accuracy academic NLP             | Slower, more accurate on academic domains   |
| MedCAT             | Medical domain (UMLS-aware)            | Required for eClinical / healthcare         |
| ScispaCy           | Scientific/biomedical NER              | Good complement to MedCAT for eClinical     |

The default deployment uses spaCy + GLiNER. eClinical adds MedCAT and
ScispaCy. The framework supports stacking models — multiple NER engines
running on the same text with their outputs merged.

### 4.4 Embedding models

| Model                          | Use                                    | Notes                                |
| ------------------------------ | -------------------------------------- | ------------------------------------ |
| OpenAI `text-embedding-3-large` | Default for cards and concepts        | Strong general performance           |
| OpenAI `text-embedding-3-small` | Cost-optimized retrieval              | Acceptable quality, much cheaper     |
| BGE-large-en-v1.5              | Open-source default                    | Competitive with OpenAI; self-hostable |
| BGE-M3                         | Multilingual, dense + sparse           | Useful for international tenants     |
| Cohere embed-v3                | Strong on semantic similarity          | Good alternative to OpenAI           |
| E5-mistral-7b-instruct         | Open-source, instruction-tuned         | Self-hosted on GPU                   |
| Domain fine-tunes              | When generic models miss domain nuance | Built later when training data accumulates |

Embedding choice is per-collection in Qdrant. Cards typically use the
strongest model; auxiliary indices (entity linking similarity, hot-path
retrieval caches) can use cheaper models.

### 4.5 LLM models

| Model              | Use                                                   |
| ------------------ | ----------------------------------------------------- |
| Claude Opus        | Causal claim extraction, identifiability prose, complex card generation |
| Claude Sonnet      | Default for most card generation, document claim extraction |
| Claude Haiku       | Routine tasks: summary generation, simple validations |
| GPT-4 / GPT-4 Turbo | Alternative for customers preferring OpenAI          |
| Llama 3 (self-hosted) | Cost-optimized standalone deployments              |
| Mistral / Mixtral  | Self-hosted alternatives                              |
| Gemini Pro         | Alternative for GCP-native customers                  |

The framework treats LLMs as pluggable. A `ModelProvider` abstraction wraps
each (Anthropic API, OpenAI API, Bedrock, vLLM-served local model). Operators
specify the *role* they need (claim_extractor_strong, validator, summarizer)
and the deployment configuration maps roles to providers.

### 4.6 Causal effect estimation models

| Method                         | Use                                          | Library     |
| ------------------------------ | -------------------------------------------- | ----------- |
| Backdoor adjustment            | Effect estimation with measured confounders  | `dowhy`     |
| Instrumental variables         | Identification with valid IV                  | `dowhy`, `linearmodels` |
| Front-door criterion            | When mediators are observed                  | `dowhy`     |
| Propensity score matching      | Quasi-experimental design                     | `dowhy`, `causalml` |
| Doubly robust estimation       | Robustness to misspecification                | `econml`    |
| Difference-in-differences      | Time-series with treatment timing             | `econml`, custom |
| Synthetic control              | Single-unit treatment evaluation              | `causalimpact` |
| Regression discontinuity       | Threshold-based assignments                   | `rdd`, `econml` |
| Heterogeneous treatment effects | Variable effect across subpopulations        | `econml`    |
| Bayesian causal inference      | Posterior distributions over effects          | `pymc`      |

These are used by the Weight Learner and by KnowQL `CAUSAL EFFECT` and
`WHAT-IF` queries. The framework supports running multiple methods in
parallel and reporting the consensus or the most-credible estimate based on
data characteristics.

### 4.7 Weight learning models

For learning causal edge weights from outcome data:

| Model                      | When                                        |
| -------------------------- | ------------------------------------------- |
| Logistic regression        | Binary outcomes, interpretable coefficients (Sameer's existing M1–M13 pattern) |
| Gradient boosted trees     | Non-linear, large feature sets               |
| Bayesian logistic regression | Priors from prior runs, posterior CIs    |
| Neural causal models       | Complex non-linear with deep features        |
| Kernel methods             | When relationships are smooth but non-linear |

Most of the existing risk modeling work uses logistic regression with
calibration, which fits the framework cleanly. The Weight Learner runs the
chosen model, extracts coefficient + bootstrap CI, and writes the result
into the `causal_edge` card's weight field.

---

## 5. Tools and Libraries

A consolidated table of the libraries the constructs use, organized by
purpose. Marked with which execution backends each library naturally fits.

| Library                    | Purpose                                | Backends                |
| -------------------------- | -------------------------------------- | ----------------------- |
| **PySpark**                | Distributed dataframe operations        | Spark                   |
| **Spark SQL**              | SQL on warehouse-scale data             | Spark                   |
| **Delta Lake**             | Versioned tables, time-travel           | Spark, standalone (delta-rs) |
| **Spark NLP** (John Snow)  | Distributed NLP including NER           | Spark                   |
| **Pandas**                 | In-memory dataframes                    | Standalone              |
| **Polars**                 | Faster Pandas alternative; lazy eval    | Standalone, Ray         |
| **DuckDB**                 | In-process SQL on Parquet/Arrow         | Standalone, Ray         |
| **PyArrow**                | Columnar interop                        | All                     |
| **Ray Core**               | Distributed Python tasks                | Ray                     |
| **Ray Datasets**           | Distributed dataframes                  | Ray                     |
| **Ray Train**              | Distributed model training              | Ray                     |
| **Ray Serve**              | Online model serving                    | Ray                     |
| **scipy / numpy**          | Numerical and statistical primitives    | All                     |
| **scikit-learn**           | ML primitives                           | All                     |
| **statsmodels**            | Statistical models, time-series          | All                     |
| **dython, phik**           | Mixed-type correlations                 | All                     |
| **causal-learn**           | PC, FGES, GES, etc.                      | All                     |
| **lingam**                 | LiNGAM family                           | All                     |
| **notears**                | NOTEARS algorithm                        | All                     |
| **tigramite**              | PCMCI for time-series causality          | All                     |
| **DoWhy**                  | Causal effect estimation                 | All                     |
| **EconML**                 | Heterogeneous treatment effects          | All                     |
| **CausalImpact**           | Bayesian time-series intervention        | All                     |
| **PyMC**                   | Bayesian modeling                        | All                     |
| **LightGBM, XGBoost**      | Gradient boosted trees                   | All (Spark variants exist) |
| **spaCy**                  | NER, POS, dependency parsing             | All (Spark via spark-spaCy) |
| **GLiNER**                 | Zero-shot domain-typed NER               | All                     |
| **MedCAT, ScispaCy**       | Medical NER                              | All (eClinical)         |
| **transformers (HF)**      | Embeddings, NER fine-tunes               | All                     |
| **sentence-transformers**  | Embedding generation                     | All                     |
| **unstructured.io**        | Document parsing                         | Standalone, Ray         |
| **LlamaIndex**             | Document loaders, chunkers               | Standalone              |
| **pdfplumber, PyPDF2**     | PDF parsing                              | Standalone              |
| **tree-sitter**            | Code AST parsing                         | All                     |
| **LangGraph**              | Workflow orchestration                   | Standalone, Ray         |
| **Anthropic SDK**          | Claude API                               | All                     |
| **OpenAI SDK**             | GPT API, embeddings                      | All                     |
| **vLLM**                   | Self-hosted LLM serving                  | Ray Serve, standalone   |
| **Qdrant client**          | Vector store access                      | All                     |
| **psycopg / SQLAlchemy**   | Postgres access                          | Standalone              |
| **Dagster, Prefect**       | Pipeline orchestration                   | All                     |
| **Airflow**                | Pipeline orchestration (legacy customers) | All                    |
| **rustworkx, NetworkX**    | In-memory graph algorithms               | All                     |
| **MLflow**                 | Model tracking, registry                  | All (native on Databricks) |

The "all" designation means the library works in every backend, but execution
patterns differ — running `causal-learn` on Spark means each tenant or each
algorithm runs on a separate Spark executor; on Ray it's a Ray task; on
standalone it's a Python subprocess.

---

## 6. The Operator Abstraction

The piece that makes three execution backends viable from a single codebase.

### 6.1 The contract

An operator is a class that declares:

- **Inputs**: typed artifact references it consumes.
- **Outputs**: typed artifact references it produces.
- **Configuration**: parameters that don't depend on the execution backend.
- **Resources**: CPU, memory, GPU, parallelism hints.
- **Idempotency key**: a deterministic hash of inputs + config that identifies
  whether this operator's output has been computed before.
- **Execution function**: the actual computation, written against an
  `ExecutionContext` that abstracts the backend.

```python
class Operator(ABC):
    name: str
    version: int
    inputs: List[ArtifactRef]
    outputs: List[ArtifactRef]
    config: OperatorConfig
    resources: ResourceHints
    
    @abstractmethod
    def execute(self, context: ExecutionContext) -> List[Artifact]:
        """The actual computation. Uses context to access data,
        write artifacts, log progress, etc."""
        ...
    
    def idempotency_key(self) -> str:
        """Deterministic hash from inputs + config + version.
        Used to skip re-execution when nothing changed."""
        ...
```

### 6.2 The execution context

The context is the abstraction that hides the backend. It exposes:

- **Read methods** for input artifacts: `context.read_dataframe(input_ref)`,
  `context.read_documents(input_ref)`, etc. These return the appropriate type
  for the backend — Pandas DataFrame on standalone, Spark DataFrame on Spark,
  Ray Dataset on Ray.
- **Write methods** for output artifacts: `context.write_artifact(output_ref,
  data)`. The backend handles materialization to the artifact store.
- **Sub-task spawning** for operators that decompose into parallel work:
  `context.parallelize(items, fn)`. Translates to RDD operations on Spark,
  multiprocessing on standalone, Ray tasks on Ray.
- **LLM and model access**: `context.llm("strong_claim_extractor", prompt)`.
  Routes to the configured provider for the role.
- **Logging and metrics**: `context.log_metric(name, value)`,
  `context.log_event(event)`.
- **Checkpoint hooks**: `context.checkpoint(state)`.

The operator author writes against the context's abstract methods. The
backend implementation provides them.

### 6.3 What the abstraction does and doesn't hide

The abstraction is honest. It hides:

- Whether data lives in Spark, Pandas, or Ray Dataset format.
- Whether parallelism uses Spark partitions, multiprocessing, or Ray tasks.
- Where the artifact store is and how it's accessed.
- Which LLM provider serves a given role.

It does **not** hide:

- Resource declarations — operators must say how much they need; the backend
  schedules accordingly.
- Latency characteristics — Spark startup is seconds; standalone is
  milliseconds; Ray is between.
- Cost characteristics — operators that use cheap models on standalone may
  use expensive ones in production; the backend selection affects this.

Operators can opt into backend-specific code paths via guards
(`if context.backend == "spark"`), but this is discouraged. The cost of
maintaining backend-specific code is real and is paid every time a new
backend is added.

### 6.4 Operator composition

Pipelines compose operators into DAGs. The framework provides:

- **Sequential composition**: operator B reads operator A's output.
- **Parallel composition**: operators B, C, D all read A's output independently.
- **Fan-out / fan-in**: A produces N items; B runs once per item; C aggregates
  B's outputs.
- **Conditional execution**: skip operator B if its idempotency key matches a
  prior run.
- **Sub-pipelines**: a pipeline can be packaged as a single operator and
  composed into a larger pipeline.

Composition is the construct used in the ingestion plan's stages — Stage 2
(Analysis) is a sub-pipeline of correlator + causal discoverer + NER + claim
extractor + entity linker, which itself can be composed into the larger
ingestion pipeline.

---

## 7. Spark Backend

The backend for customers running on Databricks, Microsoft Fabric, EMR, or
self-managed Spark. Co-located with warehouse data.

### 7.1 Architecture

A Spark backend implementation provides the `ExecutionContext` interface using
PySpark. Reads return Spark DataFrames; writes go to Delta tables (or Parquet
when Delta isn't available). Parallelism uses Spark's native partitioning.

```
┌──────────────────────────────────────────────────────────────┐
│  Spark Driver                                                 │
│                                                                │
│  Pipeline Orchestrator                                        │
│  ├─ Reads operator DAG                                        │
│  ├─ Translates each operator to Spark job                     │
│  └─ Submits jobs via SparkSession                             │
└──────────────────────────────────────────────────────────────┘
                              │
                              ▼
┌──────────────────────────────────────────────────────────────┐
│  Spark Executors                                              │
│                                                                │
│  Each operator's execute() runs in an executor task.          │
│  Heavy SQL is pushed to Spark SQL.                            │
│  Python UDFs handle the operator-specific logic.              │
└──────────────────────────────────────────────────────────────┘
                              │
                              ▼
┌──────────────────────────────────────────────────────────────┐
│  Storage                                                       │
│                                                                │
│  Delta Lake for artifacts                                     │
│  Unity Catalog for governance (Databricks)                    │
│  Workspace volumes for documents                              │
└──────────────────────────────────────────────────────────────┘
```

### 7.2 What works well in Spark

- **Profiling at warehouse scale.** A column profile across a billion-row
  table is exactly what Spark is for.
- **Correlations on tabular data.** Pairwise correlations across hundreds of
  columns parallelize cleanly.
- **Outcome stream joins.** Joining outcome tables with feature tables at
  scale is SQL-shaped work.
- **Schema introspection.** information_schema queries, dbt manifest parsing,
  Unity Catalog API calls — all natural.
- **Delta Lake versioning.** Maps onto the artifact versioning model
  beautifully — every operator output is a Delta table version.

### 7.3 What is awkward in Spark

- **NER on documents.** Spark NLP works but is heavyweight for moderate
  document volumes. Often better to run NER outside Spark and feed the
  results back in.
- **LLM calls.** Spark UDFs that call LLMs serialize poorly and can cause
  driver-executor coordination issues. The pattern that works: collect chunks
  to driver, batch LLM calls there, write results back as a DataFrame.
- **Causal discovery.** Algorithms like PC and FGES are not naturally
  partitioned. They run on a single executor as a Python process, with Spark
  used only for data movement.
- **Graph operations.** GraphX exists but is JVM-only; recursive Python graph
  work (rustworkx, NetworkX) doesn't parallelize via Spark.

### 7.4 Spark-specific design decisions

- **Use Delta Lake when available** for artifact storage. Time-travel and
  schema evolution map directly onto the artifact versioning needs.
- **Co-locate with warehouse data**. The whole point of the Spark backend is
  proximity to customer data. Cross-region data movement is a deployment
  failure.
- **Use Unity Catalog for governance** on Databricks. The Dynamic Layer's
  markings and permissions translate naturally to Unity grants.
- **Batch LLM calls at the driver** rather than per-row in UDFs. The
  framework provides a `context.batch_llm(prompts, role)` helper that
  handles this pattern.
- **Cap operator parallelism**. Spark's natural impulse is to run on every
  available core; operators that hit external services (LLMs, APIs) need
  explicit concurrency caps to respect rate limits.

### 7.5 Deployment shape

| Component             | Where                                            |
| --------------------- | ------------------------------------------------ |
| Pipeline orchestrator | Databricks job or Spark Submit                   |
| Operator code         | Python wheel installed on cluster                 |
| Artifact store        | Delta tables in Unity Catalog or workspace volumes |
| Vector store          | Databricks Vector Search or external Qdrant       |
| Card store            | Postgres-equivalent (Databricks SQL serverless or external) |
| LLM access            | API calls to Anthropic / OpenAI / Bedrock          |
| Eval framework        | Runs as separate Spark job triggered on operator output |
| Scheduling            | Databricks Workflows or external Airflow           |

---

## 8. Standalone Backend

The backend for customers without a warehouse-co-located deployment, for
development, for smaller scales, and for the reference implementation.

### 8.1 Architecture

A single Python process (or a few coordinated processes) runs the pipeline.
Pandas/Polars/DuckDB handle data; Postgres+Qdrant store cards and graphs;
LangGraph orchestrates per-operator workflows.

```
┌──────────────────────────────────────────────────────────────┐
│  Python Pipeline Process                                       │
│                                                                │
│  Pipeline Orchestrator (Dagster or in-process scheduler)      │
│  ├─ Reads operator DAG                                        │
│  ├─ Spawns operator processes / threads                       │
│  └─ Collects results                                          │
└──────────────────────────────────────────────────────────────┘
                              │
                              ▼
┌──────────────────────────────────────────────────────────────┐
│  Worker Processes                                             │
│                                                                │
│  Each operator's execute() runs as Python.                    │
│  Heavy SQL goes to DuckDB.                                    │
│  Pandas/Polars for in-memory dataframe operations.            │
│  Multiprocessing for parallel sub-tasks.                      │
└──────────────────────────────────────────────────────────────┘
                              │
                              ▼
┌──────────────────────────────────────────────────────────────┐
│  Storage                                                       │
│                                                                │
│  Postgres for artifacts, cards, audit, lineage                 │
│  Qdrant for vector store                                       │
│  Local filesystem or S3 for documents                          │
└──────────────────────────────────────────────────────────────┘
```

### 8.2 What works well standalone

- **End-to-end development.** Everything runs on a laptop; no cluster bring-up.
- **Document-heavy pipelines.** PDF parsing, NER, claim extraction are all
  natively Python — no Spark serialization tax.
- **LLM-bound work.** Most of the cost is API calls anyway; the backend is
  not the bottleneck.
- **Smaller deployments.** Tenants with thousands of cards rather than
  millions don't need distributed compute.
- **Custom and quirky data sources.** A REST API, a one-off CSV, a niche
  database — easy to integrate as a standalone source adapter.

### 8.3 What is hard standalone

- **Warehouse-scale profiling.** A billion-row profile via DuckDB is doable
  but slow. Sampling is mandatory.
- **Cross-region data.** No co-location story — data must come to the
  pipeline.
- **Concurrent multi-tenancy.** A single Python process serves limited
  concurrency; production deployments need horizontal scaling via process
  pools.

### 8.4 Standalone-specific design decisions

- **DuckDB as the SQL engine**. In-process, fast, supports Parquet and Arrow
  natively. The right choice when Postgres SQL is too slow but Spark is
  overkill.
- **Polars over Pandas where possible**. Lazy evaluation, faster, lower
  memory footprint. Pandas remains the lingua franca for libraries that
  expect it.
- **Multiprocessing for parallelism**. Within an operator, sub-tasks
  parallelize via `multiprocessing.Pool` or `concurrent.futures`. The
  context's `parallelize` helper hides the implementation.
- **Dagster for orchestration**. Strong typed-asset model, good
  observability, runs comfortably standalone or scales out. Prefect is the
  alternative if customers prefer it.

### 8.5 Deployment shape

| Component             | Where                                            |
| --------------------- | ------------------------------------------------ |
| Pipeline orchestrator | Dagster or Prefect, single instance              |
| Operator code         | Python package, possibly Dockerized               |
| Artifact store        | Postgres tables, Parquet files for large outputs |
| Vector store          | Qdrant (self-hosted or Qdrant Cloud)              |
| Card store            | Postgres                                         |
| LLM access            | API calls                                         |
| Eval framework        | Same Python process or sibling service            |
| Scheduling            | cron, Dagster schedules, or systemd timer        |

---

## 9. Ray Backend (Feature Engineering and Data Flow Extension)

Ray as the third backend, optimized for feature engineering and data flow
operators that don't fit Spark's SQL model and don't fit standalone's
single-process model.

### 9.1 Architecture

Ray Core provides distributed Python tasks and actors. Ray Datasets handle
large data flows. Ray Train scales distributed training (when the system
fine-tunes its own NER or causal models). Ray Serve hosts online inference
endpoints (e.g., entity linking as a service for low-latency call paths).

```
┌──────────────────────────────────────────────────────────────┐
│  Ray Cluster                                                  │
│                                                                │
│  Head Node                                                    │
│  ├─ Ray Dashboard, Job Submission, Object Store               │
│  └─ Pipeline Orchestrator                                     │
│                                                                │
│  Worker Nodes (auto-scaling)                                  │
│  ├─ Ray Tasks (operator execute() calls)                      │
│  ├─ Ray Actors (stateful: model servers, feature accumulators) │
│  ├─ Ray Datasets (distributed dataframes for feature pipelines) │
│  └─ Ray Train workers (when model training runs)              │
└──────────────────────────────────────────────────────────────┘
                              │
                              ▼
┌──────────────────────────────────────────────────────────────┐
│  Ray Serve (optional, for online inference)                   │
│                                                                │
│  Entity Linker Endpoint                                       │
│  Embedding Endpoint                                           │
│  NER Endpoint                                                 │
└──────────────────────────────────────────────────────────────┘
                              │
                              ▼
┌──────────────────────────────────────────────────────────────┐
│  Storage (shared with other backends)                         │
└──────────────────────────────────────────────────────────────┘
```

### 9.2 What Ray does well

- **Feature engineering at scale.** Ray Datasets handle billion-row feature
  computation with the same Pythonic API as Pandas. Window aggregators, joins,
  encoders parallelize naturally.
- **Heterogeneous workloads.** A Ray cluster can mix CPU-bound feature work,
  GPU-bound embedding generation, and memory-bound graph computation in one
  job graph.
- **Distributed model training.** Ray Train scales gradient boosting, neural
  models, and LLM fine-tuning across the cluster.
- **Online inference.** Ray Serve hosts the entity linker, embedder, and NER
  models as low-latency endpoints. Same models the batch pipeline uses, now
  servable to KnowQL's hot path.
- **Stateful actors.** Long-running processes like a feature accumulator that
  ingests outcome events and maintains rolling windows fit Ray's actor model.
- **Mixed batch and streaming.** Ray supports both; useful for feature
  pipelines that combine historical features with near-real-time updates.

### 9.3 What Ray is awkward for

- **Pure SQL workloads.** Spark wins on warehouse-scale joins; Ray is OK but
  not optimized for it.
- **Tight integration with warehouse storage.** No native Delta Lake support
  the way Spark has it. Reading Delta from Ray works via Arrow but lacks
  Spark's optimizations.
- **Mature governance.** Unity Catalog, Snowflake Horizon, Purview integrate
  better with Spark and warehouse-native tools than with Ray.

### 9.4 The feature engineering case

Why Ray is the right backend for feature engineering specifically:

A `WindowAggregator` over a 12-month window across a million entities, joining
3 outcome tables, computing 50 derived features per entity — this is the kind
of workload that wants distributed Python data, GPU access for embedding
generation as features, and the ability to checkpoint long-running state. Spark
does the SQL part well but stumbles on the Python-heavy feature transformation
part. Standalone runs out of memory. Ray Datasets handle this shape natively.

The framework provides a `FeaturePipeline` construct that composes feature
engineers with explicit:

- **Feature definitions**: name, type, lineage, refresh cadence.
- **Time semantics**: at what time horizon is this feature computed? Point-in-
  time correctness for training data, latest-available for serving.
- **Materialization strategy**: precomputed and stored, or computed on demand.
- **Refresh triggers**: time-based, event-based, manual.

Feature pipelines run as Ray jobs by default. Output is materialized to the
artifact store and (optionally) served via Ray Serve for online consumption.

### 9.5 Data flow extension

Beyond feature engineering, Ray serves as the data flow extension layer for
operators that don't fit cleanly in Spark or standalone:

- **Streaming pipelines** that ingest outcome events continuously (Ray + Kafka
  consumer).
- **Online feature serving** with sub-millisecond latency requirements (Ray
  Serve).
- **Mixed batch/online inference** — a NER service that serves the batch
  pipeline at scale and the KnowQL hot path at low latency from the same
  model deployment.
- **Custom data flow operators** that customers write — Ray's open API makes
  it the natural extension point for tenant-specific feature engineering.

### 9.6 Ray-specific design decisions

- **Ray jobs, not always-on cluster**. A Ray cluster running 24/7 is
  expensive; the framework default is "spin up Ray for the feature pipeline,
  spin down when done." Always-on is reserved for online serving via Ray
  Serve.
- **Co-existence with Spark and standalone.** Ray jobs are submitted from any
  backend; the Ray backend is a sub-component, not a replacement. The Spark
  pipeline can submit a Ray feature engineering job mid-flight.
- **Ray Datasets for the feature data layer.** Native Arrow integration means
  feature data can flow between Spark, standalone Pandas, and Ray Datasets
  with minimal serialization cost.
- **Anyscale or self-managed.** Anyscale provides managed Ray; self-managed
  is fine on Kubernetes. Customer choice; the framework is agnostic.

### 9.7 Deployment shape

| Component             | Where                                            |
| --------------------- | ------------------------------------------------ |
| Ray cluster           | Anyscale, KubeRay on customer Kubernetes, self-managed VMs |
| Feature pipeline jobs | Ray Jobs submitted from orchestrator              |
| Online inference      | Ray Serve deployments (entity linker, embedder, NER) |
| Feature store         | Materialized Ray Datasets or Feast on top of Ray   |
| Model registry        | MLflow, integrated with Ray Train                  |
| Resource autoscaling  | KubeRay autoscaler or Anyscale's native autoscaler |

---

## 10. Cross-Backend Concerns

Things that work the same regardless of backend, with backend-specific
implementations.

### 10.1 Artifact storage

Artifacts are typed, versioned, content-addressed. Each backend has a
preferred storage layer:

| Backend     | Artifact storage                                  |
| ----------- | ------------------------------------------------- |
| Spark       | Delta Lake tables, with Parquet fallback          |
| Standalone  | Postgres + Parquet on S3 or local disk            |
| Ray         | Ray Object Store for hot artifacts; Parquet/S3 for persisted |

The framework abstracts these behind an `ArtifactStore` interface. Operators
read and write through this interface; the backend provides the
implementation. Cross-backend artifact transfer (Spark output consumed by Ray
job) goes through Parquet on shared storage as the lingua franca.

### 10.2 Idempotency and incremental execution

Every operator carries an idempotency key (hash of inputs + config + version).
Before execution, the framework checks whether an artifact with that key
already exists. If yes, skip; if no, execute.

This works identically across backends. The artifact store is the truth; the
backend is execution. A pipeline that ran yesterday on standalone and is
re-run today on Spark will skip operators whose inputs haven't changed —
because the idempotency keys match and the standalone-produced artifact is
still valid.

### 10.3 Observability

| Concern              | Implementation                                       |
| -------------------- | ---------------------------------------------------- |
| Operator-level metrics | OpenTelemetry from `context.log_metric`             |
| LLM call tracing     | Langfuse or LangSmith, backend-agnostic              |
| Pipeline progress    | Dagster UI (standalone), Spark UI (Spark), Ray Dashboard (Ray), unified via OpenTelemetry export |
| Lineage              | OpenLineage events emitted from operator entry/exit  |
| Cost tracking        | Per-run cost rolled up from LLM, compute, and storage components |

The unified observability layer is OpenTelemetry. Each backend's native UI is
useful for backend-specific debugging; the unified view is what operations
teams use day-to-day.

### 10.4 Resource management

Operators declare resource hints (CPU, memory, GPU, parallelism). Each
backend interprets them:

- **Spark** translates to executor configuration and parallelism settings.
- **Standalone** uses them to size process pools and avoid oversubscription.
- **Ray** maps to Ray's `num_cpus`, `num_gpus`, and resource specifications.

GPU resources are first-class. Operators that use embeddings, NER models, or
fine-tuning declare GPU requirements; the backend schedules accordingly. On
Spark, this typically means running on GPU-enabled clusters; on standalone,
single-GPU or CPU fallback; on Ray, GPU node pools.

### 10.5 State and checkpointing

Long-running operators (causal discovery, model training) checkpoint state so
they can recover from failures without restarting. The `context.checkpoint`
hook writes checkpoint data to the artifact store; the operator reads it at
startup if resuming.

Each backend provides recovery semantics:

- **Spark**: tasks restart on executor failure; explicit checkpointing for
  driver-side state.
- **Standalone**: process restart with checkpoint reload.
- **Ray**: actor state persists across task failures; explicit checkpointing
  for shared state.

### 10.6 Configuration and secrets

Every operator reads configuration from a single source — the pipeline's
`Configuration` object passed in via the context. Secrets (API keys, DB
credentials) come from the configured secret backend (Vault, AWS Secrets
Manager, Databricks secrets, Kubernetes secrets). Operators never embed
secrets in code.

---

## 11. Worked Example: One Pipeline, Three Backends

A concrete example to make the abstraction tangible.

The pipeline: ingest a new compliance policy document, extract claims,
generate candidate cards.

**Operators** (engine-agnostic):

```
1. PdfStructuralChunker(pdf_path) → DocumentChunkArtifact
2. SpacyNER(chunks) → EntitySpanArtifact (generic types)
3. GLiNERNER(chunks, type_set) → EntitySpanArtifact (domain types)
4. NERMerge(spacy_spans, gliner_spans) → MergedEntitySpanArtifact
5. ClaimExtractor(chunks, merged_spans, "governance") → ClaimArtifact
6. EntityLinker(claim_entities) → LinkedEntityArtifact
7. CardGenerator(claims, linked_entities) → CardCandidateArtifact
```

**Standalone execution:**

```
DocumentChunker → 12 chunks (Polars DataFrame)
  ↓
SpacyNER (Python in-process, GPU if available) → 87 spans
  ↓ in parallel
GLiNERNER (Python in-process) → 312 spans
  ↓
NERMerge (Pandas join) → 391 merged spans
  ↓
ClaimExtractor (12 LLM calls, batched) → 28 claims
  ↓
EntityLinker (28 Qdrant queries) → 26 linked, 2 candidates
  ↓
CardGenerator (LangGraph workflow) → 11 candidate card edits
```

Total time: ~4 minutes on a laptop with GPU.

**Spark execution:**

```
DocumentChunker → 12 chunks (Spark DataFrame, 1 partition)
  ↓
SpacyNER (Spark NLP UDF, single executor) → 87 spans
  ↓ in parallel
GLiNERNER (Spark NLP UDF) → 312 spans
  ↓
NERMerge (Spark SQL join) → 391 merged spans
  ↓
ClaimExtractor (collected to driver, batched LLM calls) → 28 claims
  ↓
EntityLinker (Qdrant queries from driver) → 26 linked, 2 candidates
  ↓
CardGenerator (LangGraph workflow on driver) → 11 candidate card edits
```

Total time: ~6 minutes including Spark startup. Spark is overkill for one
document; the win comes when ingesting hundreds of policies in parallel.

**Ray execution:**

```
DocumentChunker → 12 chunks (Ray Dataset)
  ↓
SpacyNER (Ray task per chunk, GPU pool) → 87 spans
  ↓ in parallel
GLiNERNER (Ray task per chunk, GPU pool) → 312 spans
  ↓
NERMerge (Ray Dataset operation) → 391 merged spans
  ↓
ClaimExtractor (Ray tasks, one per chunk, batched LLM) → 28 claims
  ↓
EntityLinker (Ray Serve endpoint for hot path) → 26 linked, 2 candidates
  ↓
CardGenerator (LangGraph on driver, with Ray-served embeddings) → 11 cards
```

Total time: ~3 minutes. Ray's GPU pool runs spaCy and GLiNER in parallel on
all 12 chunks at once; the entity linker is a Ray Serve endpoint that
returns in milliseconds rather than per-call Qdrant queries.

The same operator definitions ran on all three. The execution context
varied; the logic did not.

---

## 12. Operational Concerns

### 12.1 Backend selection

How does a deployment decide which backend to use? Three signals:

1. **Where is the customer's data?** If it's in Databricks Lakehouse,
   Snowflake, or Fabric, Spark backend is the natural fit (Snowpark for
   Snowflake is treated as a Spark variant). If it's exported flat files or
   external APIs, standalone is fine. If feature engineering is the
   dominant load, Ray.

2. **What's the data scale?** Below a billion rows total, standalone with
   sampling and DuckDB is competitive. Above that, Spark or Ray pays off.

3. **What's the operational sophistication of the customer?** Spark on
   Databricks is "we already run a Databricks." Ray cluster operations are
   "we already run Kubernetes and have an ML platform team." Standalone is
   "we deploy a Docker container."

The three are not exclusive. A production deployment might use Spark for the
heavy profiling and correlation work, standalone for document and code
ingestion, and Ray for feature pipelines and online inference. The framework
supports this — the pipeline orchestrator submits each operator to the right
backend.

### 12.2 Multi-backend pipelines

Pipelines that mix backends are first-class. A typical shape:

```
Spark (warehouse profile)
  → Standalone (document ingestion)
  → Ray (feature engineering)
  → Standalone (card generation, LLM-bound)
```

Cross-backend artifact transfer goes through Parquet on shared object storage
(S3, GCS, ADLS). The framework handles it transparently — operator B reading
operator A's output doesn't need to know that A ran on Spark and B runs on
Ray.

### 12.3 Cost control

LLM calls dominate cost across all backends. Spark and Ray add compute cost;
standalone is cheapest at small scale. Cost controls are operator-level:

- **Tiered model assignment** by operator and tenant (cheap models for low-
  stakes work).
- **Cache by idempotency key** — never re-run an operator whose inputs and
  config haven't changed.
- **Batch LLM calls** at the operator level rather than per-row.
- **Sample-then-cover** for very large inputs.
- **Approximate algorithms** in profilers (HLL, t-digest).

The framework reports cost per pipeline run, broken down by operator. Cost
budgets per pipeline are enforceable — an operator that exceeds its budget
is killed and its output marked failed.

### 12.4 Failure handling

Operator failures are typed:

| Failure type           | Handling                                              |
| ---------------------- | ----------------------------------------------------- |
| Transient (network, rate limit) | Retry with backoff, max 3 attempts            |
| Resource exhaustion    | Increase resource hint, retry once; if still fails, fail run |
| Validation failure     | Fail operator; don't retry; surface for review        |
| Code bug (unhandled exception) | Fail run; pipeline rolls back partial work; alert |
| Upstream failure       | Skip downstream; mark dependent operators as not-run   |

Each backend has different retry semantics — Spark tasks retry transparently
on task-level failures; Ray actors restart; standalone retries via the
orchestrator. The framework normalizes these into the typed failure model.

### 12.5 Testing

The operator abstraction makes testing tractable. Operators can be tested in
isolation with a mock execution context that records reads/writes. Pipelines
can be tested end-to-end with the standalone backend on fixtures (real data,
small scale). Backend-specific behavior is tested with backend-specific
integration tests, but the operator logic is tested once.

---

## 13. Tooling Summary

A consolidated table of recommended tools per concern. This is the shopping
list for builders.

| Concern                    | Recommendation                                       |
| -------------------------- | ---------------------------------------------------- |
| **Pipeline orchestration** | Dagster (typed assets) or Prefect; Airflow if customer requires |
| **Operator framework**     | Custom — built on top of LangGraph for sub-workflows |
| **Spark deployment**       | Databricks (managed) or Spark on Kubernetes (self-managed) |
| **Standalone runtime**     | Python 3.11+, Docker, Postgres 15+, Qdrant 1.7+      |
| **Ray cluster**            | Anyscale (managed) or KubeRay on Kubernetes          |
| **SQL engine (standalone)** | DuckDB                                              |
| **Dataframe (standalone)** | Polars (preferred), Pandas (compatibility)           |
| **Document parsing**       | unstructured.io, LlamaIndex readers, pdfplumber      |
| **Code parsing**           | tree-sitter with language-specific grammars          |
| **NER**                    | spaCy (en_core_web_trf) + GLiNER + custom rules      |
| **Embeddings**             | text-embedding-3-large or BGE-large-en-v1.5          |
| **LLM access**             | Anthropic SDK (primary), OpenAI SDK (alternative), vLLM (self-hosted) |
| **Vector store**           | Qdrant                                               |
| **Causal discovery**       | causal-learn, lingam, notears, tigramite             |
| **Causal effect estimation** | DoWhy, EconML, CausalImpact, PyMC                  |
| **Statistical**            | scipy, statsmodels, dython, phik                     |
| **ML primitives**          | scikit-learn, LightGBM, XGBoost                      |
| **Workflow**               | LangGraph (for agentic sub-workflows)                |
| **Observability**          | OpenTelemetry + Langfuse for LLM tracing             |
| **Data lineage**           | OpenLineage emitted from operators                    |
| **Model registry**         | MLflow                                               |
| **Secrets**                | Vault, AWS Secrets Manager, Databricks secrets       |
| **Object storage**         | S3, GCS, ADLS, or local filesystem for development   |

---

## 14. Open Design Questions

1. **Operator versioning across backends.** When an operator's logic changes,
   its idempotency key changes, and the artifact store recomputes. But what
   about operators with backend-specific implementations that diverge? The
   default answer: idempotency key includes operator version but not backend.
   If two backends produce different outputs from the same inputs, that's a
   bug — backends should produce equivalent artifacts. Worth enforcing with
   cross-backend tests.

2. **Streaming vs batch.** The current design is batch-oriented with daily
   incremental runs. Some operators (outcome streams, real-time feature
   updates) want streaming semantics. Ray supports both; Spark Streaming
   exists; standalone needs more thought. The streaming surface area is
   probably worth deferring until a customer demands it.

3. **GPU scheduling across backends.** GPU-bound operators (embeddings, NER,
   model training) need different scheduling on each backend. Spark with
   GPU-enabled clusters works; Ray's GPU resource model works; standalone
   needs explicit GPU coordination. A consistent declaration model exists,
   but the implementation maturity differs.

4. **Customer-written operators.** The Ray extension story implies customers
   can write their own feature engineers and data flow operators. What's the
   API surface and the security model? Probably starts as Python plugins
   with a constrained interface; later evolves to a more managed model with
   sandboxing.

5. **Backend selection automation.** Right now, the deployment chooses the
   backend per operator. Could it be automatic — the framework picks based
   on data size, operator characteristics, and available resources? Probably
   yes, but only after enough deployment experience to encode the heuristics.
   Start manual; automate later.

6. **Cross-backend artifact compatibility.** Artifacts produced on one
   backend should be consumable on another. The Parquet+Arrow lingua franca
   handles this for tabular data. For graph artifacts (causal DAG candidates,
   entity span sets), the serialization format needs more thought. Probably
   JSON for small artifacts, custom binary for large ones, with explicit
   schema versioning.

7. **Spark vs Snowpark vs Fabric.** Each warehouse has its own Spark variant.
   The Spark backend should be portable across them with minimal code
   changes. Worth designing the Spark backend with adapter abstractions for
   warehouse-specific APIs (Unity Catalog vs Snowflake Horizon vs Purview).

8. **Online inference scaling.** Ray Serve handles low-latency serving for
   the hot paths (entity linker, NER for KnowQL), but production
   deployments may want more sophisticated inference platforms (Triton,
   BentoML). The framework should support pluggable inference backends, not
   require Ray Serve.

---

## 15. Phased Delivery

**Phase 1 — Standalone reference implementation.** Operator framework,
artifact store, pipeline orchestrator (Dagster), all constructs running in
standalone mode. Postgres + Qdrant + DuckDB + LangGraph. End: end-to-end
pipeline produces cards from a fixture data source, fully testable, fully
observable. ~6 weeks.

**Phase 2 — Spark backend.** PySpark `ExecutionContext` implementation, Delta
Lake artifact storage, Spark NLP integration where useful. Cross-backend
artifact compatibility verified. End: same pipeline runs on Databricks, with
warehouse-co-located profiling and correlation operators showing meaningful
speedup. ~4 weeks.

**Phase 3 — Ray backend.** Ray Core + Ray Datasets, feature engineering
constructs (`WindowAggregator`, `JoinFeaturizer`, `EmbeddingFeaturizer`,
`GraphFeaturizer`), Ray Serve for entity linker. End: feature pipelines run
distributed; entity linking is sub-millisecond from KnowQL. ~4 weeks.

**Phase 4 — Cross-backend pipelines.** Pipeline orchestrator submits
operators to different backends within a single run. Multi-backend artifact
transfer optimized. End: production pipelines mix backends naturally.
~2 weeks.

**Phase 5 — Domain pack support.** Constructs accept domain-specific
configuration (NER type sets, causal edge priors, golden datasets, ontology
adapters for MedDRA/CWE/etc.). End: same extraction framework supports both
LMS+security and eClinical packs. ~3 weeks.

**Phase 6 — Production hardening.** GPU scheduling consistency, online
inference patterns, cost budget enforcement, multi-tenant isolation across
backends, customer-extension API for Ray operators. End: production-ready,
multi-tenant, multi-backend deployment. ~4 weeks.

Each phase delivers usable functionality; the system is shippable from Phase
1 onward. Spark and Ray backends are additive — they don't gate standalone.
Total to GA: ~23 weeks from start, with usable internal deployments much
earlier.
