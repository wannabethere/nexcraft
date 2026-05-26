from __future__ import annotations

from ontology_foundry.models import EntitySpan

# Higher base wins when resolving overlaps (§3.6 — prefer specific types).
TYPE_SPECIFICITY: dict[str, float] = {
    "causal_marker": 120.0,
    "quantitative_claim": 95.0,
    "concept": 85.0,
    "attribute": 82.0,
    "event": 80.0,
    "actor_role": 78.0,
    "policy_reference": 76.0,
    "temporal_qualifier": 74.0,
    "entity_name": 72.0,
    "PERSON": 55.0,
    "ORG": 55.0,
    "GPE": 52.0,
    "DATE": 50.0,
    "MONEY": 50.0,
    "CARDINAL": 45.0,
    "PROPER_NOUN": 40.0,
}


def _score(span: EntitySpan) -> float:
    base = TYPE_SPECIFICITY.get(span.span_type, 60.0)
    return base + span.confidence


def merge_entity_spans(spans: list[EntitySpan]) -> list[EntitySpan]:
    """
    Resolve overlaps by keeping the highest composite score per overlapping group
    (greedy global ordering by score, then non-overlapping).
    """
    if not spans:
        return []
    ordered = sorted(spans, key=lambda s: (-_score(s), s.char_start, s.char_end))
    kept: list[EntitySpan] = []
    for span in ordered:
        if any(
            not (span.char_end <= k.char_start or span.char_start >= k.char_end) for k in kept
        ):
            continue
        kept.append(span)
    kept.sort(key=lambda s: s.char_start)
    return kept
