# Evaluation Strategy — Groundedness, Correctness, and Hallucination

How the system measures and enforces quality at every stage where
generation, inference, or reasoning happens. The strategy treats quality
as something that requires continuous measurement, not a one-time gate.

---

## 1. The Three Concerns

Quality in this system has three distinct dimensions. They overlap but
are not the same, and each requires different eval techniques.

| Concern         | Question being answered                                        |
| --------------- | -------------------------------------------------------------- |
| **Groundedness** | Is every claim traceable to a specific source — a card, a chunk, a finding, a row? |
| **Correctness** | Is every claim actually true given the source — does the card or response match what the source says? |
| **Hallucination prevention** | Has the LLM invented entities, relationships, quantities, or citations that don't exist? |

A card can be grounded but incorrect (the source it cites is wrong). It
can be correct but ungrounded (the claim happens to be true but no source
is cited). And hallucinations are a specific failure mode where the LLM
fabricates content even though no source supports it.

The eval strategy addresses each concern with its own checks, applied at
the right stages.

---

## 2. Where Evals Run

Evals run at five distinct points, with different cost and rigor at each.

| Point                        | Style                                  | Cost     | Coverage          |
| ---------------------------- | -------------------------------------- | -------- | ----------------- |
| **Pre-publish gates**        | Programmatic, must-pass                | Cheap    | Every card        |
| **Post-publish sampling**    | LLM-judge + programmatic               | Moderate | Sampled fraction  |
| **Continuous monitoring**    | Aggregated metrics, drift detection    | Cheap    | All traffic       |
| **Periodic regression**      | Golden datasets, full suite            | Expensive | Weekly/release    |
| **Human review**             | Domain experts, structured rubrics     | Most expensive | Sampled, HITL queue |

The general rule: cheap, mechanical checks at every stage; expensive
checks on samples; human review only where the others can't reach.

---

## 3. Card-Level Pre-Publish Gates

These run on every candidate card before it can be published. A failed
gate either retries generation or routes to HITL.

### 3.1 Schema and structure

| Check                                      | Method                                  | Failure handling          |
| ------------------------------------------ | --------------------------------------- | ------------------------- |
| Header schema valid for kind                | Pydantic validation                     | Fail and retry generation |
| Required fields present                     | Schema check                            | Fail and retry            |
| Header types match (weight is float, etc.)  | Schema check                            | Fail and retry            |
| Body within length cap (warn 600, fail 1000)| Tokenizer count                         | Warn → split flow; fail → retry shorter |
| Body is non-empty                           | Length check                            | Fail and retry            |
| ID follows naming convention                | Regex                                   | Fail and retry            |

### 3.2 Reference resolution

| Check                                             | Method                                          |
| ------------------------------------------------- | ----------------------------------------------- |
| Every entry in `refs` resolves to an existing card | Lookup in card store                            |
| Every entity named in body resolves via NER+linker | NER pass + entity linker (covered in pipeline §7.2) |
| No dangling refs after edit (other cards still link to this) | Reverse-reference index check                   |
| Causal edges reference real `causal_node` cards    | Targeted lookup                                 |

### 3.3 Provenance

| Check                                          | Method                                          |
| ---------------------------------------------- | ----------------------------------------------- |
| Every card has at least one source provenance entry | Header check                                    |
| Provenance points to actual chunks, findings, or rows | Lookup in source artifact store                 |
| Causal edge weights have an accompanying evidence batch | Header `weight.source` + linked finding         |
| Numeric claims in body match values in linked findings | Regex extract + numerical comparison            |

### 3.4 Internal consistency

| Check                                               | Method                                          |
| --------------------------------------------------- | ----------------------------------------------- |
| Header values match body claims (weight value, CI)   | Programmatic — extract from body, compare to header |
| No contradiction within the body                    | LLM-judge with structured rubric                 |
| Causal direction in body matches edge direction      | LLM-judge                                        |
| Identifiability prose matches header flag            | LLM-judge — does the prose argue for what the flag claims? |

The four LLM-judge calls in this section are the most expensive part of
pre-publish. They're worth it because they catch the failure mode where
the LLM writes coherent prose that subtly contradicts the header — the
hardest hallucination to spot mechanically.

---

## 4. Cross-Card Consistency Gates

Run after card-level gates, before publish. These check that the new
card doesn't break the corpus.

| Check                                                       | Method                                          |
| ----------------------------------------------------------- | ----------------------------------------------- |
| No causal cycle introduced                                  | DAG cycle check on causal graph                  |
| Linked cards don't contradict the new card                  | LLM-judge over neighborhood subset              |
| Embedding-similarity to existing concepts < threshold       | Vector similarity over concept collection        |
| If card replaces an old version, diff is sensible           | LLM-judge: "is this a reasonable evolution?"    |
| Reverse-reference graph stays consistent                    | Programmatic — every ref bidirectional          |

The "linked cards don't contradict" check is bounded — it doesn't compare
against every card in the store, only the immediate neighborhood (refs
out, refs in, top-k embedding-similar). Beyond that the cost balloons
without proportional value.

---

## 5. Source-Grounding Suite

A specific battery of evals focused on whether claims are anchored to
sources. Runs as part of pre-publish for new cards and periodically on
sampled existing cards.

### 5.1 Span-level grounding

For each claim in a card's body:

1. **Find the supporting span.** Extract the claim, search for it in the
   linked sources via embedding similarity + lexical overlap.
2. **Verify the span exists.** Did NER actually flag the entities in the
   claim within that span?
3. **Verify the quantities match.** If the claim mentions "40% reduction",
   the source span must contain "40" or an equivalent expression — exact
   match required for numbers, semantic match accepted for qualitative
   phrasing.
4. **Score grounding strength.** A claim with verbatim source quote scores
   higher than one paraphrased; a claim with no traceable span fails.

### 5.2 Citation accuracy

For each `refs` entry in a header:

1. The referenced card must exist.
2. The referenced card must contain content actually relevant to the
   claim being made — verified via LLM-judge with structured rubric.
3. Stale refs (referenced card was deprecated since last edit) flagged
   and the citing card routed for review.

### 5.3 Quantitative integrity

Numeric claims get extra scrutiny:

| Claim type            | Validation                                              |
| --------------------- | ------------------------------------------------------- |
| Causal weight         | Must match Weight Learner output exactly                 |
| Confidence interval   | Must match Weight Learner output exactly                 |
| Sample count `n`      | Must match the underlying evidence batch                 |
| Effect percentage     | Must round-trip to within 5% of source                   |
| Date or temporal qualifier | Must match policy or rule source verbatim          |
| Counts and aggregates  | Must match warehouse query as of the run timestamp     |

Quantitative integrity failures are never auto-fixed; they always route
to HITL because the right answer depends on which source is correct.

---

## 6. Causal-Specific Evals

Causal claims have failure modes that other claims don't, and they need
specialized checks.

### 6.1 Identifiability checking

The Identifiability Checker is itself a checker — but its outputs need
verification:

| Check                                              | Method                                       |
| -------------------------------------------------- | -------------------------------------------- |
| Claimed adjustment set actually closes back-door paths | Run do-calculus engine with stated adjustment set |
| Claimed confounders exist as causal_nodes          | Lookup                                       |
| If "instrumental variable" is claimed, the IV satisfies the IV criteria | Programmatic                                |
| If "do-calculus admissible" claimed, attempt the derivation | Run derivation; if fails, flag             |

### 6.2 Weight calibration

For learned weights:

| Check                                                  | Method                                       |
| ------------------------------------------------------ | -------------------------------------------- |
| Weight estimate falls within bootstrap CI of refit on holdout | Statistical refit on held-out slice         |
| Sign of weight is stable across last 3 refits           | Compare weight history                       |
| CI width has narrowed monotonically as `n` grew         | Time-series check on weight history          |
| No sudden flip in sign without explanatory event        | Anomaly detection; flag for review           |

### 6.3 Counterfactual sanity

For high-stakes causal edges:

1. Run the edge through dowhy-style refutation tests (placebo treatment,
   random common cause, data subset stability).
2. Compare predicted effect under intervention to a held-out
   intervention dataset where available.
3. Flag any edge that fails refutation; demote to hypothesized.

### 6.4 Hypothesized → learned promotion eval

The promotion sub-pipeline (covered in the ingestion plan) uses
thresholds — but the thresholds themselves need eval. Periodically:

1. Audit promoted edges that later got demoted (false-positive promotions).
2. Audit hypothesized edges that languish without being promoted despite
   strong evidence (false-negative non-promotions).
3. Use these to calibrate the promotion thresholds.

---

## 7. Hallucination Detection

Hallucinations have a distinct taxonomy. Each kind needs a specific
detection method.

| Hallucination type            | Example                                              | Detection                                       |
| ----------------------------- | ---------------------------------------------------- | ----------------------------------------------- |
| **Fabricated entity**         | Card mentions "PhishingResistance" but no such concept exists | NER + entity linker — fails at validation       |
| **Fabricated relationship**   | Card claims X causes Y but no `causal_edge` exists   | Reverse-ref check during validation             |
| **Fabricated quantity**       | Card claims weight of 0.85 when learner says 0.62    | Quantitative integrity check (§5.3)             |
| **Fabricated citation**       | Card cites `policy_2024.md` but that doc doesn't exist or doesn't say that | Source-grounding span check (§5.1)              |
| **Plausible-but-wrong inference** | Card claims a confounder is admissible when it's not | LLM-judge with adversarial probe; identifiability checker |
| **Outdated assertion**        | Card claims something that was true but isn't anymore | Drift detection on source provenance            |
| **Confabulated reasoning chain** | Card argues identifiability via a chain of false steps | LLM-judge with chain-of-thought audit           |
| **Stylistic confabulation**   | Card uses plausible domain language for nonsense     | LLM-judge with domain-expert rubric             |

The first four are caught mechanically. The bottom four need LLM-judge or
human review. None can be eliminated entirely — the goal is to catch them
before they propagate, and to detect them quickly when they slip through.

### 7.1 Adversarial probe suite

A maintained set of prompts designed to elicit hallucinations:

- **Counter-evidence prompts.** Ask the LLM to confirm a claim that
  contradicts the source. A grounded system refuses or qualifies.
- **Made-up entity prompts.** Inject an entity name that doesn't exist
  ("does the FrobnicateRisk concept apply here?"). A grounded system
  says it doesn't recognize the entity.
- **Unanswerable questions.** Ask for information the corpus genuinely
  doesn't contain. A grounded system says so rather than confabulating.
- **Pressure prompts.** Add urgency or authority to the prompt
  ("urgent — confirm this for the audit"). A grounded system maintains
  its standards.

The probe suite runs nightly against the live system. Failure rate is a
KPI.

### 7.2 Confidence calibration

A grounded system knows when it's uncertain. Calibration evals check
whether the system's stated confidence matches its actual accuracy:

1. Take 1,000 KnowQL queries with known answers.
2. Bucket the responses by stated confidence (high / medium / low or
   numeric).
3. Compute accuracy within each bucket.
4. Plot the calibration curve.

A well-calibrated system has accuracy roughly equal to confidence in each
bucket. Miscalibration in either direction is a problem — overconfident
wrong answers are dangerous, underconfident right answers are unhelpful.

---

## 8. KnowQL Response Evals

The card store quality is one thing; the quality of responses produced
from it is another. Response evals address the layer between cards and
user.

### 8.1 Retrieval quality

For a query, did the planner retrieve the right cards?

| Metric            | Definition                                              |
| ----------------- | ------------------------------------------------------- |
| Context precision | Fraction of retrieved cards that are actually relevant  |
| Context recall    | Fraction of relevant cards that were retrieved          |
| Card kind accuracy | Did the planner retrieve from the right card kinds?    |
| Layer accuracy    | Did the planner pull from the right layers?             |

Measured against a gold set where reviewers have annotated which cards
are relevant for each query. Ragas provides good baseline metrics here.

### 8.2 Response faithfulness

Once cards are retrieved, does the response stay faithful to them?

- **Faithfulness (Ragas)**: every claim in the response can be inferred
  from at least one retrieved card.
- **Citation accuracy**: every citation in the response points to a
  retrieved card that actually contains the cited content.
- **Coverage**: the response addresses what was asked, not adjacent
  topics retrieved alongside.
- **Completeness**: the response doesn't leave out relevant retrieved
  content the user would need.

### 8.3 Causal-query specific

For `CAUSAL EFFECT` and `WHAT-IF` queries:

- The reported weight matches the underlying causal_edge card.
- The reported CI matches.
- The identifiability claim matches the card's flag.
- The attribution percentages sum to 100%.
- Edges with `weight.source: hypothesized` are explicitly flagged in the
  response (the user must know they're seeing a hypothesis, not a
  finding).

### 8.4 Permission filtering accuracy

Critical. A response that leaks data the role isn't permitted to see is
worse than a wrong response.

- 100% pass rate on permission-leak probes (queries that would expose
  PII to non-PII-cleared roles).
- Audit log every query with the role and the retrieved card set;
  sample audit entries weekly for manual review.

Permission filtering tests are the only category where a single failure
is treated as a critical incident, not a metric.

---

## 9. Golden Datasets and Regression

### 9.1 Maintained golden datasets

| Dataset                    | What it contains                                          |
| -------------------------- | --------------------------------------------------------- |
| **Card golden set**        | 200 cards with reviewer-vetted quality labels; used for pre-publish gate calibration |
| **KnowQL query golden set** | 500 queries with reference responses and citation expectations |
| **Causal claim golden set** | 100 causal edges with known-correct weights from controlled experiments or published literature |
| **Hallucination probe set** | 200 adversarial prompts, refreshed quarterly                |
| **Permission probe set**    | 100 queries designed to test permission boundaries        |

These datasets are the regression suite. Every release runs against them.

### 9.2 Regression gates

A release cannot ship if:

- Card golden set quality drops by > 2 percentage points.
- KnowQL faithfulness drops by > 1 percentage point.
- Causal claim accuracy drops at all.
- Hallucination probe failure rate increases at all.
- Any permission probe leaks data.

The asymmetry — bigger tolerance on quality metrics, zero tolerance on
hallucinations and permissions — reflects that some failures are
recoverable and some are not.

### 9.3 Golden set maintenance

Golden sets go stale. Quarterly review:

- Domain experts re-label a 10% sample of card golden set; if disagree
  with old labels, dataset is updated.
- KnowQL query golden set is augmented with queries from production
  traffic that were marked "incorrect" by users.
- Hallucination probe set is rotated — old probes the system has solved
  graduate to a "regression-only" suite; new probes designed for
  current weak spots take their place.
- Causal claim golden set is updated as new outcome data arrives.

---

## 10. Continuous Monitoring

Production traffic generates data that supplements offline evals.

### 10.1 Real-time metrics

| Metric                           | Source                                | Alert threshold                        |
| -------------------------------- | ------------------------------------- | -------------------------------------- |
| Card publish gate failure rate   | Pre-publish gate logs                 | > 5% in 1 hour                         |
| HITL queue depth by kind         | HITL queue                            | Causal review queue > 20 pending       |
| Response faithfulness (sampled)  | LLM-judge on sampled responses        | < 95% rolling 24h                      |
| Hallucination probe failure rate | Nightly probe runs                    | Any increase                           |
| Permission filter check failure  | Every query                           | Any single failure                     |
| Card-graph drift                 | Drift sampler                         | > 0.1% drift                           |
| Causal weight stability          | Weight learner outputs                | Sign flip without explanatory event    |

### 10.2 Drift detection

Specific drift signals worth monitoring:

- **Source drift**: a card's source provenance now points to content that
  has changed since the card was generated. Indicates the card may need
  refresh.
- **Concept drift**: queries that used to retrieve a card no longer do
  (or vice versa). Indicates the card's embedding has drifted from its
  query patterns.
- **Causal drift**: a causal edge weight is changing rapidly across
  refits. Indicates the underlying relationship may be non-stationary.
- **Vocabulary drift**: NER is finding new entity types that weren't
  seen before. Could be legitimate growth or could be noise.

Drift is not failure on its own; it's a signal that something needs
attention.

### 10.3 User feedback as eval signal

Two channels:

- **Explicit feedback**: thumbs-up/down on KnowQL responses, with
  optional text. Aggregates into per-card and per-query quality
  signals.
- **Implicit feedback**: user reformulations (asking the same thing
  differently within a session) suggest the first response was
  inadequate.

Both feed into golden set maintenance.

---

## 11. Human Review

For everything mechanical and LLM-judge methods can't catch — and as
ground truth for tuning the automated evals.

### 11.1 What gets reviewed

| Category                                | Cadence              |
| --------------------------------------- | -------------------- |
| HITL queue items (new concepts, new causal edges, large weight changes) | As they arrive |
| Random sample of auto-published cards   | 1% sampled, weekly   |
| Sampled KnowQL responses                | 100 per week, stratified by kind |
| Disputed cards (user feedback flagged)  | All within 48 hours  |
| Permission filter audit                 | Monthly              |

### 11.2 Reviewer rubrics

Each review category has a structured rubric:

**Card review rubric:**
- Is the card grounded? (every claim traceable)
- Is the card correct? (claims match sources)
- Is the card well-scoped? (not too narrow, not too broad)
- Is the prose clear and free of confabulation?
- Score: accept / accept-with-edits / reject

**Causal edge review rubric:**
- Is the direction correct?
- Is the weight reasonable given the evidence?
- Is the identifiability claim valid?
- Are the confounders exhaustive?
- Score: accept / accept-with-edits / reject / demote

**KnowQL response review rubric:**
- Is the response faithful to retrieved cards?
- Are citations accurate?
- Is uncertainty communicated when appropriate?
- Score: 1–5 plus free-text feedback

Rubric scores are the data the LLM-judges are calibrated against.
Disagreement between reviewer and LLM-judge is a signal that the judge
prompt needs tuning.

### 11.3 Reviewer pool

Two tiers:

- **Domain reviewers**: subject-matter experts (compliance officers, ML
  engineers, security analysts). Handle high-stakes reviews — causal
  claims, governance changes, disputed responses.
- **General reviewers**: trained ontology contributors. Handle routine
  card reviews and KnowQL response sampling.

Inter-rater agreement is measured monthly. Rubrics are revised when
agreement drops.

---

## 12. Tooling

| Need                                 | Recommendation                                       |
| ------------------------------------ | ---------------------------------------------------- |
| RAG evaluation framework             | Ragas (faithfulness, context precision/recall)       |
| LLM evaluation framework             | DeepEval or Promptfoo for structured suites          |
| LLM tracing & observability          | Langfuse or LangSmith                                |
| Adversarial probe management         | Custom — versioned probe sets in Git                 |
| Golden dataset versioning            | DVC or lakeFS for dataset versioning                 |
| Metric collection                    | Prometheus + Grafana, or Datadog                     |
| Calibration plotting                 | Custom on matplotlib, or Aim                         |
| Causal-specific testing              | dowhy (refutation tests), `causal-learn`             |
| Statistical refit and bootstrapping  | scipy, statsmodels                                   |
| Reviewer UI                          | Custom — diff view, rubric capture, queue management  |
| A/B harness for prompt comparisons   | Promptfoo or Anthropic Workbench                     |

---

## 13. Operational SLOs

The targets the system commits to. Missing them triggers escalation.

| SLO                                          | Target  | Measurement window |
| -------------------------------------------- | ------- | ------------------ |
| Card pre-publish gate pass rate              | > 95%   | Rolling 7 days     |
| KnowQL response faithfulness                 | > 97%   | Rolling 7 days     |
| Hallucination probe pass rate                | > 99%   | Rolling 30 days    |
| Permission filter failure rate               | 0       | Always             |
| Card golden set quality                      | > 90%   | Per release        |
| Mean HITL review turnaround                  | < 24h   | Rolling 7 days     |
| Causal claim accuracy on golden set          | > 85%   | Per release        |
| Card-graph drift                             | < 0.1%  | Weekly             |

The two zero-tolerance SLOs (permission filter, hallucination on
permission probes) are the only ones that don't tolerate a single
failure. Everything else has a budget.

---

## 14. Open Questions

1. **LLM-judge reliability.** LLM-judges drift as models change and have
   known biases (length bias, confidence bias). Calibrate them quarterly
   against human reviewers, but the right cadence depends on how often
   the underlying model changes.

2. **Cost of comprehensive evaluation.** The full pre-publish gate
   battery on every card is expensive at corpus scale. The trade-off is
   gate strictness vs throughput. Probably stratify: tighter gates on
   high-stakes cards (causal_edge, governance), looser on low-stakes
   (property_type updates).

3. **Counterfactual ground truth for causal claims.** Without controlled
   experiments, validating causal weights against truth is hard. Best
   approximations: holdout slice refits, refutation tests, comparison
   to published literature where available. When the system makes a
   causal claim that can't be ground-truthed, the response needs to
   communicate that uncertainty.

4. **Hallucination probe coverage.** The probe set is finite; the
   hallucination space is infinite. Pass rate on the probe set is a
   leading indicator, not a guarantee. Worth periodically asking domain
   experts to design new probe categories targeting weak spots.

5. **Reviewer fatigue.** A 1% sample of weekly publishes might be
   thousands of cards. Reviewers degrade in accuracy under volume.
   Rotating reviewer assignments and limiting per-reviewer daily volume
   are standard mitigations but cost throughput.

6. **Continuous evaluation vs release-gated evaluation.** Some teams run
   the full eval suite continuously; others gate on releases. The
   right answer depends on release cadence — for daily incremental
   pipeline runs, continuous monitoring is more useful than release
   gates. For monthly platform releases, release gates dominate. Both
   probably have a place.

7. **Ground-truth maintenance cost.** Golden datasets need quarterly
   refresh. As the corpus grows, the proportional cost of golden
   maintenance grows with it. Consider whether some golden subsets can
   be auto-maintained (e.g., causal claims that have been validated by
   subsequent outcome data).

---

## 15. What Ships First

Like the other plans, a phased rollout that delivers value early and
hardens over time.

**Phase 1 — Mechanical gates.** Schema validation, ref resolution,
length checks, NER-based entity validation, basic provenance checks.
These are cheap, deterministic, and catch the most common failure modes.
End: cards can't publish without basic structural integrity.

**Phase 2 — Source grounding.** Span-level grounding for new cards,
quantitative integrity checks, citation accuracy. End: every card claim
is traceable to a source.

**Phase 3 — LLM-judge layer.** Internal consistency checks, contradiction
detection, identifiability prose validation. Adds cost but catches
failure modes structural checks miss. End: cards pass higher quality
bar, with measurable judge accuracy against human reviewers.

**Phase 4 — Hallucination detection.** Adversarial probe suite, calibration
evals, confidence reporting. End: hallucination rates are measurable and
trending.

**Phase 5 — Continuous monitoring.** Production metrics, drift detection,
user feedback ingestion. End: quality trends are visible in real time;
regressions are caught quickly.

**Phase 6 — Causal-specific.** Identifiability validation, weight
calibration, counterfactual sanity. End: causal claims have specialized
quality gates appropriate to their stakes.

**Phase 7 — Human review at scale.** Reviewer UI, rubric tooling,
inter-rater agreement tracking. End: human review is sustainable and
calibrates the automated layers.

Each phase is end-to-end shippable. Phases 1–2 are baseline; 3–4 raise
quality; 5 makes quality observable; 6–7 specialize and scale. The first
four phases can deliver in 6–10 weeks; the last three are long-tail
investments that pay off over quarters.
