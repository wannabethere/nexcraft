# Evaluation Harness — Specification

**Status:** Draft 2026-05-15.
**Part of:** Data Knowledge Hierarchy series.
**Depends on:** all prior specs in the series.
**Leverages (heavily):**
- `ontology_foundry.eval.gates` — `gate_nonempty_body`, `gate_id_pattern`, `gate_refs_resolve`.
- `ontology_foundry.eval.causal_checks` — `check_path_shapley_sum`, `check_reported_weight_matches_card`, `directed_graph_has_cycle`.
- `ontology_foundry.eval.grounding` — `score_span_grounding`, `check_quantitative_integrity`, `extract_numbers`, `numbers_aligned`, `lexical_overlap_score`.
- `ontology_foundry.eval.retrieval_metrics` — `context_precision_recall`.
- `ontology_foundry.eval.regression` — `regression_gate_quality`, `regression_gate_zero_tolerance`.
- `ontology_foundry.eval.models` — `EvalIssue`, `GateVerdict`, `RegressionGateReport`, `SpanGroundingResult`, `RetrievalMetricsResult`, `QuantitativeIntegrityResult`, `HallucinationProbeCase`, `CausalResponseCheckResult`.

The existing `eval/` package implements most of the primitives this spec needs. The harness is primarily **wiring** — a question corpus, a runner, a set of regression gates, and the specific evals motivated by the hierarchy + cards + retrieval design.

---

## 1. Scope

This spec defines:

1. The **question corpus** structure and curation process — the ground-truth set against which retrieval and answer quality are measured.
2. The **three core evals** the system must pass:
   - **Eval 1: Context Sufficiency** — does retrieval pull enough cards/bundles to answer?
   - **Eval 2: Answer Quality vs. Baseline** — does cards+bundles beat alternative configurations?
   - **Eval 3: Drift Resilience** — when MDL changes, does the pipeline flag it?
3. The **graduated-detail policy evals** specific to the depth-budget + branching-cap policy.
4. The **regression gate** wired into CI to prevent retrieval/quality regressions on PR merges.
5. The **bench** for performance targets from `bundle_consumer_api_spec.md` §12.

Out of scope:
- Algorithm-specific evals (causal-discovery quality is a separate concern, partially covered by existing `eval/causal_checks.py`).
- Customer-facing eval reports (operational dashboards, not specced here).

---

## 2. Question corpus

The corpus is the **artifact** of the harness. Without it, the harness is just code. Curation is a one-time investment that pays back across every spec change.

### 2.1 Structure

One YAML file per question. Layout:

```
tests/eval/questions/
  causal/
    q001_training_to_attrition.yaml
    q002_phishing_to_incidents.yaml
    ...
  governance/
    q050_phi_assets_in_scope.yaml
    ...
  compliance_rec/
    q080_hipaa_dashboard_recommendations.yaml
    ...
  dashboard_rec/
    q120_revenue_anomaly_explainers.yaml
    ...
  schema_lookup/
    q160_what_columns_in_encounters.yaml
    ...
  entity_resolution/
    q190_employee_across_sources.yaml
    ...
```

### 2.2 Per-question schema

```yaml
id: q001_training_to_attrition
intent: causal_reasoning
question: |
  Why might increased training completion rates correlate with reduced
  attrition in clinical departments?
anchors_expected:
  - asset_rk: snowflake://acme-prod.csod/public/csod_employee
  - asset_rk: snowflake://acme-prod.csod/public/training_assignment
required_cards:
  - { kind: object_type, id: employee, distance_required: 0_or_1 }
  - { kind: object_type, id: training_assignment, distance_required: 0_or_1 }
  - { kind: causal_node, id: overdue_risk, distance_required: 0_or_2 }
  - { kind: causal_node, id: compliance_gap, distance_required: 0_or_3 }
  - { kind: object_type, id: department, distance_required: 0_or_2 }
sufficient_cards:        # min set that should be present to answer well
  - employee
  - training_assignment
  - overdue_risk
  - compliance_gap
preferred_bundle_concerns:
  - mdl
  - bindings
  - causal
expected_answer_signals:
  - mentions: ["overdue", "training assignment", "attrition", "department"]
  - cites_claims:
      - { subject_ref: "Employee.training_completion_rate",
          predicate: "leading_indicator_of",
          object_ref: "compliance_gap" }
  - quantitative:
      - { value_range: [0.0, 1.0], unit: "ratio",
          near_text: "training completion rate" }
forbidden_in_answer:
  - "I don't have enough information"          # the corpus must be sufficient for this question
hardness: medium
domain_tags: [Clinical, HR, Compliance]
labelled_by: jane.k@acme.com
labelled_at: 2026-05-10
```

### 2.3 Curation rules

- Every question is **answerable** with the corpus that exists when it's labelled. Questions that probe "what would the system say if X were true" are out of scope for this corpus.
- `required_cards` lists every card the answer must surface in some form (full body, summary, or manifest). `distance_required` is the **maximum** distance — cards must be at or closer than this hop count.
- `sufficient_cards` is the minimal subset for a competent answer.
- `expected_answer_signals` are textual + structural checks (mentions, claim citations, quantitative ranges). These drive the Answer Quality eval.
- `hardness ∈ {easy, medium, hard}` is used by the harness to weight regressions (a regression on hard questions is more concerning than on easy ones).
- **Distribution target:** 70% answerable at hop ≤ 1, 25% needing hops 2-3, 5% needing 4+. Validates the depth-budget default.

### 2.4 Corpus size and growth

- **Initial corpus:** 50 questions across the six intents (~8 per intent).
- **Maturity:** 200 questions before declaring eval stable.
- **Growth:** every customer-reported answer-quality issue produces a new question.

---

## 3. Eval 1: Context Sufficiency

> Does the retrieval/loader policy surface enough cards and bundles to answer correctly, before measuring whether the LLM uses them?

### 3.1 Procedure

For each question Q:

1. Run `OntologyContextLoader.load(anchors=Q.anchors_expected, intent=Q.intent, policy=current_policy)`.
2. Collect the union of cards in `ctx.cards_full ∪ ctx.cards_summary ∪ ctx.cards_manifest` and the union of bundles in `ctx.bundles`.
3. For every entry in `Q.required_cards`:
   - Check the card is present.
   - Check the card's actual distance ≤ `distance_required`.
   - Check the card's representation tier is allowed (full for `distance ≤ max_hops_full`, etc.).
4. For every `Q.preferred_bundle_concerns`: check each anchor's bundle has the concern loaded.

### 3.2 Metrics

- **`sufficiency@required`** — fraction of `required_cards` present at the right distance.
- **`sufficiency@sufficient`** — fraction of `sufficient_cards` present at *any* tier.
- **`over_pull_ratio`** — `cards_in_context / sufficient_cards_count`. Watch for over-fetching.

### 3.3 Gate thresholds

- `sufficiency@required` >= 0.95 (95% of required cards present at the right distance).
- `sufficiency@sufficient` >= 0.99 (nearly always have the minimum set).
- `over_pull_ratio` <= 4.0 (we tolerate up to 4x the minimum to allow context for the LLM; beyond is wasteful).

PR merges fail if `sufficiency@required` regresses by > 0.02 from the prior baseline.

### 3.4 Implementation

```python
# ontology_foundry/eval/sufficiency.py (to be added)

def eval_context_sufficiency(
    *,
    corpus: QuestionCorpus,
    loader: OntologyContextLoader,
    policies: dict[ContextIntent, ContextPolicy],
) -> ContextSufficiencyReport:
    ...
```

Returns a per-question result + aggregates. Reuses `ontology_foundry.eval.retrieval_metrics.context_precision_recall` internally (each question is one "query"; required_cards are the relevance labels).

---

## 4. Eval 2: Answer Quality vs. Baseline

> Conditional on the context being sufficient, does the cards+bundles configuration produce better answers than alternative configurations?

### 4.1 Configurations compared

| Config | Context source | Notes |
|---|---|---|
| `(A)` cards + bindings + bundles | Full `OntologyContextLoader` output | The system as designed. |
| `(B)` bundles only | `bundle_store.get_bundle()` for anchors; no cards | Tests whether cards add value. |
| `(C)` MDL only | Just the asset's `mdl.json`; no context.json, no bindings, no cards | Schema-only baseline. |
| `(D)` flat dump | Concatenation of all bundle contents for the anchor + immediate neighbors | Tests whether structure helps vs. raw blob. |

### 4.2 Procedure

For each question Q × each config C:

1. Build the prompt: question text + assembled context per C.
2. Send to the LLM (use `ontology_foundry.llm.OpenAIChatProvider` or another configured provider). Same model, same temperature across configs.
3. Capture the answer text.
4. Apply automated checks:
   - **Mentions check** — answer contains each token in `Q.expected_answer_signals.mentions`.
   - **Claim citation check** — answer references each entry in `Q.expected_answer_signals.cites_claims` (via `RelationArtifact`-shaped tuple match against extracted citations).
   - **Quantitative check** — `check_quantitative_integrity` on extracted numbers vs `Q.expected_answer_signals.quantitative`.
   - **Forbidden phrases** — no entry from `Q.forbidden_in_answer` appears.
   - **Span grounding** — `score_span_grounding` against the context (answers must be supported by the context, not hallucinated).
5. Aggregate per question into a composite quality score (weighted: claim citation > span grounding > mentions > quantitative).

### 4.3 Metrics

- **`quality_per_config[c]`** — mean composite quality across the corpus, weighted by `hardness`.
- **`quality_delta`** — `quality[A] - quality[B]`, `quality[A] - quality[C]`, `quality[A] - quality[D]`.

### 4.4 Gate thresholds

- `quality[A] - quality[B] >= 0.05` — cards add meaningful value over bundles alone.
- `quality[A] - quality[C] >= 0.15` — cards + bundles substantially beat MDL-only.
- `quality[A] > quality[D]` (any positive margin) — structure beats flat dump.

If `quality[A]` doesn't beat `quality[C]` by ≥ 0.15, the entire indirection is suspect — the spec gets revisited.

### 4.5 Cost

The LLM-in-the-loop nature makes Eval 2 the expensive one. Default cadence:
- Per PR: subset of 20 questions, all four configs. ~80 LLM calls.
- Nightly: full corpus, configs A + C only. ~400 LLM calls.
- Weekly: full corpus, all configs. ~800 LLM calls.

Token costs at 4o pricing for a 200-question corpus, full sweep weekly: O($30/week). Cheap relative to development time.

---

## 5. Eval 3: Drift Resilience

> When upstream MDL or storage changes, does the foundry pipeline catch the change and update / flag bindings correctly?

### 5.1 Procedure

Synthetic mutations + observation. A fixture creates a baseline state, then applies one of the following mutations and runs the relevant downstream pipeline:

| Mutation | Expected response |
|---|---|
| Rename a column in source DDL (re-run ingest) | `binding_drift_flag.kind='field_missing_in_asset'` on the affected asset's bindings |
| Add a new column in source DDL | `binding_drift_flag.kind='unbound_field_added'` on the asset |
| Bump a card's version (edit body affecting bindings) | `binding_drift_flag.kind='card_version_drift'` on every asset referencing the card |
| Remove a schema in source | `schema_ext.lifecycle_stage='removed'` cascaded; orphan lineage edges deactivated |
| Edit organization's `compliance_regimes` | All asset `context.json` regenerated within the bundle emission lag |
| Pack card update | Tenant cards overriding the pack ref still resolve; no spurious drift flags |

### 5.2 Metrics

- **`drift_detection_rate`** — fraction of mutations whose expected flag is raised within 2 minutes of the mutation.
- **`false_positive_rate`** — fraction of stable-state runs that raise spurious flags. Target: 0.
- **`mean_time_to_detect`** — wall-clock between mutation and flag insertion.

### 5.3 Gate thresholds

- `drift_detection_rate >= 0.99`.
- `false_positive_rate == 0` (strict).
- `mean_time_to_detect <= 120s`.

### 5.4 Implementation

```python
# ontology_foundry/eval/drift.py (to be added)

@dataclass
class DriftScenario:
    name: str
    setup: Callable[[], None]
    mutate: Callable[[], None]
    expected_flag_kind: str
    expected_count: int = 1
    max_wait_seconds: int = 120

def eval_drift_resilience(scenarios: list[DriftScenario], *,
                          store: HierarchyStore) -> DriftResilienceReport:
    ...
```

Scenarios live in `tests/eval/drift_scenarios/` as Python factories. The harness runs them serially against an isolated test database; cleanup between runs.

---

## 6. Graduated-detail policy evals

Specific to the depth-budget + branching-cap + token-budget mechanics from `bundle_consumer_api_spec.md` §4.

### 6.1 Depth distribution validation

For the current corpus, compute the distribution of *minimum* `distance_required` across `required_cards`:

```
hop 0: X% (anchor card itself)
hop 1: Y%
hop 2: Z%
hop 3: W%
hop ≥ 4: V%
```

Gate: target distribution within 5 percentage points of the design target (70/25/5 across hop ≤1 / 2-3 / 4+).

If the corpus drifts toward deeper hops, either the policy's `max_hops_*` should rise, or the corpus is testing unrealistic questions. Surface a warning; don't auto-fail.

### 6.2 Branching-cap pressure

For each question, record whether `demotions_applied` is non-empty. Aggregate:

- **`branching_cap_hit_rate`** — fraction of questions where the cap fired.
- **`demotion_information_loss`** — for demoted cards, did the question end up in the `forbidden_in_answer` failure mode? Proxy: per-question quality where demotion fired vs. where it didn't.

Gate: if `demotion_information_loss > 0.10`, the cap is too aggressive for the question distribution; flag for review.

### 6.3 Token budget headroom

Distribution of `estimated_tokens` across the corpus. Target: P95 ≤ 80% of `token_budget`. If P95 hits 95%+, the budget is constraining — raise it or tighten policies upstream.

### 6.4 Manifest utility

For cards appearing at distance 3 (manifest tier), measure how often the LLM's answer mentions any of them. Low mention rate suggests the manifest tier isn't adding value; high rate suggests it's effective at expanding the LLM's vocabulary.

Metric: **`manifest_mention_rate`** — fraction of answers containing at least one card id from the manifest tier.

---

## 7. Regression gate

CI gate wired into PR merges. Builds on `ontology_foundry.eval.regression`.

### 7.1 Baseline storage

A baseline file `tests/eval/baseline.json` stores the last green metrics:

```json
{
  "baseline_version": "1.0",
  "captured_at": "2026-05-15T...",
  "captured_at_commit": "abc1234",
  "context_sufficiency": {
    "sufficiency_at_required":  0.974,
    "sufficiency_at_sufficient": 0.998,
    "over_pull_ratio":           2.4
  },
  "answer_quality": {
    "quality_A": 0.81,
    "quality_B": 0.74,
    "quality_C": 0.61,
    "quality_D": 0.69
  },
  "drift": {
    "drift_detection_rate":  1.00,
    "false_positive_rate":   0.00,
    "mean_time_to_detect":   42
  },
  "perf": {
    "context_load_p95_ms_causal":     980,
    "context_load_p95_ms_compliance": 2100
  }
}
```

### 7.2 Per-PR procedure

```python
# tools/eval/run_regression.py

prev = load_baseline("tests/eval/baseline.json")
curr = run_full_eval(...)

issues = regression_gate_quality(prev, curr, thresholds={
    "sufficiency_at_required":  -0.02,    # max allowed drop
    "quality_A":                -0.03,
    "quality_A_minus_C":        -0.05,    # cards-and-bundles vs MDL-only margin
    "drift_detection_rate":     -0.01,
    "false_positive_rate":       0.00,    # zero-tolerance
    "context_load_p95_ms_causal": +200,   # max allowed increase
})

if issues:
    exit_with_failure(issues)

write_baseline_if_better(prev, curr, output="tests/eval/baseline.json")
```

`regression_gate_quality` and `regression_gate_zero_tolerance` exist in `ontology_foundry/eval/regression.py`; the harness composes them.

### 7.3 Baseline updates

Baselines are advanced only on PR merges to main that *improve* metrics. Reductions never advance the baseline. Operators can force-advance with a manual override commit.

---

## 8. Hallucination probes

Reusing `ontology_foundry.eval.models.HallucinationProbeCase`. The harness ships a set of probes — questions whose context deliberately *lacks* the answer — to verify the system says "I don't have enough information" rather than fabricates.

### 8.1 Probe structure

```yaml
id: probe001_no_phi_data
question: What is patient John Doe's diagnosis history?
context_setup:
  exclude_concerns: [bindings, causal]      # strip relevant context
  exclude_assets: [encounters, diagnoses]
expected_answer_kind: refusal_or_qualifier
forbidden_in_answer:
  - "John Doe"
  - any concrete patient identifier
```

### 8.2 Gate

- **`hallucination_rate`** — fraction of probes where the LLM fabricated rather than refused. Target: 0.0; gate at `<= 0.02`.

---

## 9. Card-specific evals (reusing existing primitives)

The card gates from `ontology_foundry.eval.gates` run on every card edit (per `semantic_layer_card_spec.md` §10). Aggregated into a corpus-level health check:

| Metric | Source | Target |
|---|---|---|
| Cards with empty body | `gate_nonempty_body` failures | 0 |
| Cards with bad id pattern | `gate_id_pattern` failures | 0 |
| Dangling refs across corpus | `gate_refs_resolve` failures | 0 |
| Cards with cycles in `extends` | (new gate) | 0 |
| Cards with `card_version_drift` flags > 30 days old | `binding_drift_flag` query | 0 |

These are dashboard metrics; CI hard-fails on any > 0.

### 9.1 Causal DAG health

`directed_graph_has_cycle` on the `(causal_node, subject_refs, outcome_refs)` graph. Cycles are flagged but don't auto-fail (feedback loops can be legitimate); flagged cycles become review-queue items.

`check_path_shapley_sum` and `check_reported_weight_matches_card` run when causal weight estimates are present, validating that reported aggregate weights match the underlying path attributions.

---

## 10. Performance bench

Tracks the targets from `bundle_consumer_api_spec.md` §12.

### 10.1 Procedure

A perf bench runs nightly:
1. Fixture: tenant with 5,000 assets + 500 cards (synthetic or anonymized real).
2. For each operation in §12 of the consumer spec, generate 100 random keys and time the call.
3. Report P50, P95, P99.

### 10.2 Gate

P95 must not regress > 20% relative to the baseline. Failures surface to ops; PRs that cause regression fail CI.

---

## 11. Eval orchestration

```
ontology_foundry/eval/
  ... existing files ...
  harness.py                # the runner that ties everything together
  sufficiency.py            # NEW — Eval 1
  drift.py                  # NEW — Eval 3
  perf_bench.py             # NEW — perf
  hallucination.py          # NEW — probes
  corpus_loader.py          # NEW — reads tests/eval/questions/

tests/eval/
  questions/                # the corpus (YAML files)
  drift_scenarios/          # Python factories for Eval 3
  hallucination_probes/     # YAML probes
  baseline.json             # the regression baseline
  baseline_history/         # archived baselines
```

`harness.py` is the single entry point. CI invokes:

```bash
python -m ontology_foundry.eval.harness \
  --suites context_sufficiency,answer_quality_subset,drift,perf \
  --baseline tests/eval/baseline.json \
  --report-out artifacts/eval_report.json
```

Nightly cron runs the full sweep with all suites and a wider corpus.

---

## 12. Reuse map (recap)

| Existing in `ontology_foundry/eval/` | Used by this spec for |
|---|---|
| `gate_nonempty_body` | §9 card health |
| `gate_id_pattern` | §9 card health |
| `gate_refs_resolve` | §9 card health |
| `directed_graph_has_cycle` | §9.1 causal DAG |
| `check_path_shapley_sum` | §9.1 |
| `check_reported_weight_matches_card` | §9.1 |
| `score_span_grounding` | §4.2 answer-quality span check |
| `check_quantitative_integrity` | §4.2 quantitative check |
| `extract_numbers`, `numbers_aligned` | §4.2 quantitative check |
| `lexical_overlap_score` | §4.2 mentions check support |
| `context_precision_recall` | §3.4 sufficiency P/R |
| `regression_gate_quality` | §7.2 PR regression gate |
| `regression_gate_zero_tolerance` | §7.2 zero-tolerance metrics |
| `EvalIssue`, `GateVerdict` | All gate returns |
| `RegressionGateReport` | §7 reports |
| `SpanGroundingResult` | §4.2 |
| `RetrievalMetricsResult` | §3.4 |
| `QuantitativeIntegrityResult` | §4.2 |
| `HallucinationProbeCase` | §8 |
| `CausalResponseCheckResult` | §9.1 |

The harness adds ~5 new files (~600 LOC total) on top of the existing eval package. Most "new" work is corpus curation.

---

## 13. Maturity ladder

Not all evals need to be green from day one. Practical sequencing:

| Phase | Goal | Required evals green |
|---|---|---|
| **Alpha** (~50 questions) | Validate architecture isn't wrong | Eval 1 (sufficiency); card gates |
| **Beta** (~100 questions) | Validate cards earn their keep | Eval 1 + Eval 2 A vs C delta |
| **GA** (~200 questions) | Full coverage | All evals; regression gate on PRs |

Spec changes between phases require corpus expansion *before* the spec change lands, not after.

---

## 14. Open items

- **Multi-LLM evaluation** — currently one model under test. Future: matrix across models (claude-opus vs gpt-4o vs llama-3) to detect model-specific over-fitting.
- **Real-user query replay** — production query logs replayed against historical bundle snapshots, comparing the system's answer at the time of the query vs. what it would say with the current bundle state. Defer until production traffic exists.
- **Synthetic question generation** — LLM-generated questions seeded by sampling cards + bundles, to scale the corpus beyond hand-curation. Defer; risk of corpus-LLM-correlation overstating quality.
- **Cross-org benchmark** — once multi-tenant, compare eval metrics per tenant to surface "this tenant's hierarchy/bindings are weaker than average" signals. Defer.

---

## 15. Change log

| Date | Change |
|---|---|
| 2026-05-15 | Initial draft. |
