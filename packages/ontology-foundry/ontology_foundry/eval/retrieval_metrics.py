from __future__ import annotations

from ontology_foundry.eval.models import RetrievalMetricsResult


def context_precision_recall(
    retrieved_ids: set[str],
    gold_relevant_ids: set[str],
) -> RetrievalMetricsResult:
    """eval_strategy §8.1 — precision / recall over card IDs."""
    if not retrieved_ids:
        return RetrievalMetricsResult(
            context_precision=0.0,
            context_recall=0.0 if gold_relevant_ids else 1.0,
            retrieved_count=0,
            relevant_retrieved=0,
            gold_relevant_count=len(gold_relevant_ids),
        )
    inter = retrieved_ids & gold_relevant_ids
    prec = len(inter) / len(retrieved_ids)
    rec = len(inter) / len(gold_relevant_ids) if gold_relevant_ids else 1.0
    return RetrievalMetricsResult(
        context_precision=prec,
        context_recall=rec,
        retrieved_count=len(retrieved_ids),
        relevant_retrieved=len(inter),
        gold_relevant_count=len(gold_relevant_ids),
    )
