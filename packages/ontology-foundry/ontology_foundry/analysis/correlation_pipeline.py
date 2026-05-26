from __future__ import annotations

from typing import Literal

from ontology_foundry.analysis.models import CandidatePairArtifact, NumericColumnProfile


ColumnSemanticType = Literal["numeric", "categorical", "text", "id", "timestamp", "unknown"]


def type_compatible(
    a: ColumnSemanticType,
    b: ColumnSemanticType,
) -> bool:
    """Tier 1 type-level filter — extraction §3.3."""
    if a == "unknown" or b == "unknown":
        return True
    if a == "text" and b == "numeric":
        return False
    if a == "numeric" and b == "text":
        return False
    if a == "id" and b == "id":
        return False
    return True


def cardinality_prefilter_drop(profile: NumericColumnProfile) -> bool:
    """Tier 1 cardinality — high null / constant / near-unique IDs."""
    if profile.null_rate > 0.95:
        return True
    if profile.distinct_count is not None and profile.n_rows > 0:
        frac_distinct = profile.distinct_count / profile.n_rows
        if frac_distinct <= 0.01:
            return True
        if frac_distinct >= 0.99 and profile.null_rate < 0.5:
            return True
    return False


def emit_candidate_pair(
    column_a: str,
    column_b: str,
    *,
    profiles: dict[str, NumericColumnProfile],
    types: dict[str, ColumnSemanticType] | None = None,
    allowed_schema_pairs: set[tuple[str, str]] | None = None,
    qualified_split_a: tuple[str, str] | None = None,
    qualified_split_b: tuple[str, str] | None = None,
    seed_prior_boost: bool = False,
) -> CandidatePairArtifact | None:
    """
    Tier 1 pre-filter for one pair. Returns ``None`` when dropped before Tier 2.
    ``qualified_split`` optional ``(table, column)`` for schema-level CDM allowlists.
    """
    types = types or {}
    ta = types.get(column_a, "unknown")
    tb = types.get(column_b, "unknown")
    if not type_compatible(ta, tb):
        return None

    pa = profiles.get(column_a)
    pb = profiles.get(column_b)
    if pa is not None and cardinality_prefilter_drop(pa):
        return None
    if pb is not None and cardinality_prefilter_drop(pb):
        return None

    if allowed_schema_pairs is not None and qualified_split_a and qualified_split_b:
        table_a, _ = qualified_split_a
        table_b, _ = qualified_split_b
        forward = (table_a, table_b)
        backward = (table_b, table_a)
        if forward not in allowed_schema_pairs and backward not in allowed_schema_pairs:
            return None

    return CandidatePairArtifact(
        column_a=column_a,
        column_b=column_b,
        priority=1.0 + (0.5 if seed_prior_boost else 0.0),
        seed_prior_boost=seed_prior_boost,
        diagnostics={"semantic_type_a": ta, "semantic_type_b": tb},
    )


def fdr_bh_correct(p_values: list[float], *, alpha: float = 0.05) -> tuple[list[float], list[bool]]:
    """
    Tier 2 FDR — extraction §3.3 ``statsmodels.stats.multitest.multipletests``.
    """
    try:
        from statsmodels.stats.multitest import multipletests
    except ImportError as e:
        raise ImportError(
            "FDR correction requires statsmodels. Install ontology-foundry[timeseries] or statsmodels."
        ) from e

    _, p_adj, _, _ = multipletests(p_values, alpha=alpha, method="fdr_bh")
    rejected = [p <= alpha for p in p_adj]
    return list(p_adj), rejected


def effect_threshold_for_n(n: int, *, floor: float = 0.1) -> float:
    """extraction §3.3 Tier 2 — max(0.1, 2/sqrt(n))."""
    import math

    return max(floor, 2.0 / math.sqrt(max(n, 1)))
