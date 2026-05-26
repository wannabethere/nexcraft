from __future__ import annotations

import re
from typing import Iterable

from ontology_foundry.eval.models import QuantitativeIntegrityResult, SpanGroundingResult


def token_set(text: str) -> set[str]:
    return {t.lower() for t in re.findall(r"[A-Za-z0-9%]+", text)}


def lexical_overlap_score(a: str, b: str) -> float:
    """Normalized Jaccard over alphanumeric tokens (cheap proxy for §5.1)."""
    sa, sb = token_set(a), token_set(b)
    if not sa and not sb:
        return 1.0
    inter = len(sa & sb)
    union = len(sa | sb)
    return inter / union if union else 0.0


def extract_numbers(text: str) -> list[float]:
    """Pull numeric literals including percentages (40% → 40.0)."""
    out: list[float] = []
    for m in re.finditer(
        r"(?:~?\d+(?:\.\d+)?)\s*%|\b\d+(?:\.\d+)?\b",
        text,
        flags=re.IGNORECASE,
    ):
        s = m.group(0).replace("%", "").strip().lstrip("~")
        try:
            out.append(float(s))
        except ValueError:
            continue
    return out


def numbers_aligned(
    claim_nums: Iterable[float],
    source_nums: Iterable[float],
    *,
    rel_tol: float = 0.05,
    abs_tol: float = 1e-6,
) -> bool:
    """§5.1 — each claim number must appear in source within relative tolerance."""
    src = list(source_nums)
    if not list(claim_nums):
        return True
    for c in claim_nums:
        if not any(abs(c - s) <= max(abs_tol, rel_tol * max(abs(c), abs(s), 1e-9)) for s in src):
            return False
    return True


def score_span_grounding(
    claim_text: str,
    source_span: str,
    *,
    min_lexical: float = 0.15,
    strength_lexical_weight: float = 0.5,
) -> SpanGroundingResult:
    """
    Battery from eval_strategy §5.1 (mechanical subset): overlap + numeric alignment.
    """
    lex = lexical_overlap_score(claim_text, source_span)
    cn = extract_numbers(claim_text)
    sn = extract_numbers(source_span)
    aligned = numbers_aligned(cn, sn)
    strength = strength_lexical_weight * lex + (1.0 - strength_lexical_weight) * (
        1.0 if aligned else 0.0
    )
    passed = lex >= min_lexical and aligned
    return SpanGroundingResult(
        claim_text=claim_text,
        source_span=source_span,
        lexical_overlap=lex,
        numbers_in_claim=cn,
        numbers_in_source=sn,
        numbers_aligned=aligned,
        grounding_strength=strength,
        passed=passed,
    )


def check_quantitative_integrity(
    claim_value: float | None,
    reference_value: float | None,
    *,
    relative_tolerance: float = 0.05,
    absolute_tolerance: float = 1e-6,
) -> QuantitativeIntegrityResult:
    """§5.3 — effect percentage round-trip style check."""
    if claim_value is None or reference_value is None:
        return QuantitativeIntegrityResult(
            claim_value=claim_value,
            reference_value=reference_value,
            relative_tolerance=relative_tolerance,
            absolute_tolerance=absolute_tolerance,
            aligned=False,
        )
    diff = abs(claim_value - reference_value)
    tol = max(absolute_tolerance, relative_tolerance * max(abs(reference_value), 1e-9))
    return QuantitativeIntegrityResult(
        claim_value=claim_value,
        reference_value=reference_value,
        relative_tolerance=relative_tolerance,
        absolute_tolerance=absolute_tolerance,
        aligned=diff <= tol,
    )
