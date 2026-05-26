"""Pure-function retrieval metrics.

No side effects, no external deps — just math over rank-ordered hit lists vs.
relevance ground truth. Used by `HistoricalComparisonScorer`. Each function
documents its definition + edge cases.
"""
from __future__ import annotations

import math
from typing import Iterable, Sequence


def precision_at_k(
    retrieved_in_order: Sequence[str],
    *,
    relevant: Iterable[str],
    k: int,
) -> float:
    """Fraction of top-k retrieved items that are in the relevant set.

    P@k = |{retrieved[:k]} ∩ relevant| / k

    Returns 0.0 when k == 0 or retrieved is empty.
    """
    if k <= 0 or not retrieved_in_order:
        return 0.0
    rel = set(relevant)
    top = retrieved_in_order[:k]
    hits = sum(1 for r in top if r in rel)
    return hits / k


def recall_at_k(
    retrieved_in_order: Sequence[str],
    *,
    relevant: Iterable[str],
    k: int,
) -> float:
    """Fraction of relevant items captured in the top-k.

    R@k = |{retrieved[:k]} ∩ relevant| / |relevant|

    Returns 0.0 when the relevant set is empty (no meaningful recall to compute).
    """
    rel = set(relevant)
    if not rel:
        return 0.0
    if k <= 0 or not retrieved_in_order:
        return 0.0
    top = retrieved_in_order[:k]
    hits = sum(1 for r in top if r in rel)
    return hits / len(rel)


def mean_reciprocal_rank(
    retrieved_in_order: Sequence[str],
    *,
    relevant: Iterable[str],
) -> float:
    """Reciprocal rank of the first relevant retrieved item.

    MRR for one query = 1/rank(first_relevant). Returns 0.0 if none found.

    Note: this returns a per-query reciprocal rank, not a mean over many
    queries. The aggregate "mean" comes from averaging this across cases in
    the scorer.
    """
    rel = set(relevant)
    if not rel or not retrieved_in_order:
        return 0.0
    for idx, item in enumerate(retrieved_in_order, start=1):
        if item in rel:
            return 1.0 / idx
    return 0.0


def discounted_cumulative_gain(
    retrieved_in_order: Sequence[str],
    *,
    relevance_grades: dict[str, int] | None = None,
    relevant: Iterable[str] | None = None,
    k: int | None = None,
) -> float:
    """DCG@k. Supports graded relevance via `relevance_grades` mapping rk -> grade.

    Falls back to binary (1.0 for items in `relevant`, 0.0 otherwise) when no
    explicit grades are supplied.

    DCG@k = sum_{i=1..k} (2^rel_i - 1) / log2(i + 1)
    """
    items = retrieved_in_order if k is None else retrieved_in_order[:k]
    if not items:
        return 0.0
    grades = dict(relevance_grades or {})
    if not grades and relevant is not None:
        grades = {r: 1 for r in relevant}
    total = 0.0
    for i, rk in enumerate(items, start=1):
        grade = grades.get(rk, 0)
        if grade <= 0:
            continue
        total += (2 ** grade - 1) / math.log2(i + 1)
    return total


def ndcg(
    retrieved_in_order: Sequence[str],
    *,
    relevance_grades: dict[str, int] | None = None,
    relevant: Iterable[str] | None = None,
    k: int | None = None,
) -> float:
    """Normalized DCG@k = DCG / ideal_DCG.

    The ideal ranking places the highest-grade items first; ties broken
    arbitrarily.

    Returns 0.0 when no graded items exist.
    """
    grades = dict(relevance_grades or {})
    if not grades and relevant is not None:
        grades = {r: 1 for r in relevant}
    if not grades:
        return 0.0

    # Ideal ranking — descending grades over the full grade dictionary
    ideal_items = [rk for rk, _ in sorted(grades.items(), key=lambda kv: kv[1], reverse=True)]
    ideal_dcg = discounted_cumulative_gain(
        ideal_items, relevance_grades=grades, k=k,
    )
    if ideal_dcg <= 0:
        return 0.0
    actual = discounted_cumulative_gain(
        retrieved_in_order, relevance_grades=grades, k=k,
    )
    return actual / ideal_dcg


def hit_rate(
    retrieved_in_order: Sequence[str],
    *,
    relevant: Iterable[str],
    k: int | None = None,
) -> float:
    """1.0 if ANY relevant item appears in retrieved[:k], else 0.0.

    Per-case binary signal — averaged across cases this becomes the fraction
    of cases that landed at least one relevant hit.
    """
    rel = set(relevant)
    if not rel:
        return 0.0
    top = retrieved_in_order if k is None else retrieved_in_order[:k]
    return 1.0 if any(r in rel for r in top) else 0.0


def forbidden_violations(
    retrieved_in_order: Sequence[str],
    *,
    forbidden: Iterable[str],
    k: int | None = None,
) -> int:
    """Count retrieved items that appear in the forbidden set (top-k slice)."""
    f = set(forbidden)
    if not f:
        return 0
    top = retrieved_in_order if k is None else retrieved_in_order[:k]
    return sum(1 for r in top if r in f)
