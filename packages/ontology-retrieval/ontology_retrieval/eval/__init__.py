"""Retrieval evaluation — scorers + background worker.

Two scoring modes today:
  - `historical_comparison`  — deterministic metrics (P@k, R@k, MRR, nDCG, hit_rate)
                                against `eval_case.expected_asset_rks`.
  - `llm_judge`              — per-item rating + holistic coverage rating via LLM,
                                aggregated into mean / pass-rate metrics.

The `EvalWorker` polls `eval_run` rows in `pending` status, executes both
scorers per case (configurable), persists per-case `eval_result` rows, and
rolls up aggregates into `eval_metric`.

Entry points:
  - `EvalWorker.run_pending()`         — pull pending runs and execute (cron-friendly).
  - `EvalWorker.execute_run(run_id)`   — execute a specific run id.
  - `Scorer.score(case, retrieved)`    — pure scoring; usable outside the worker.

CLI:
  - `ontology-retrieval eval run-pending`
  - `ontology-retrieval eval run-once --kind asset_search`
  - `ontology-retrieval eval status`
  - `ontology-retrieval eval import-cases --path ./eval_cases.yaml`
"""
from ontology_retrieval.eval.metrics import (
    discounted_cumulative_gain,
    hit_rate,
    mean_reciprocal_rank,
    ndcg,
    precision_at_k,
    recall_at_k,
)
from ontology_retrieval.eval.scorers import (
    HistoricalComparisonScorer,
    LLMJudgeScorer,
    Scorer,
    ScorerResult,
)
from ontology_retrieval.eval.worker import EvalWorker, EvalWorkerStats

__all__ = [
    "Scorer",
    "ScorerResult",
    "HistoricalComparisonScorer",
    "LLMJudgeScorer",
    "EvalWorker",
    "EvalWorkerStats",
    "precision_at_k",
    "recall_at_k",
    "mean_reciprocal_rank",
    "discounted_cumulative_gain",
    "ndcg",
    "hit_rate",
]
