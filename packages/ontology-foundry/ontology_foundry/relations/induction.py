"""Induce a `RelationSchema` from a corpus of extracted `RelationArtifact`s.

Two passes:
  1. Cluster predicate surfaces into canonical names. The LLM is biased toward
     using seed names as the canonical form when a cluster member matches; this
     is what keeps the induced TBox aligned with hand-authored vocabulary.
  2. Aggregate (subject_type, object_type) per canonical predicate; the
     majority types become the schema's domain/range.

`novel_promotion_candidates()` is the feedback loop: novel predicates with
enough corpus support become seed candidates for the next pack iteration.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from typing import Iterable

from ontology_foundry.llm.provider import ModelProvider, ModelRole
from ontology_foundry.llm.transform import llm_structured_transform
from ontology_foundry.models import RelationArtifact
from ontology_foundry.relations.artifacts import CanonicalizationResponse
from ontology_foundry.relations.schema import RelationSchema, RelationType
from ontology_foundry.relations.seeds import RelationSeed, SeedPack


@dataclass
class InducedPredicate:
    """Audit record for one canonical predicate after induction."""

    canonical: str
    surfaces: tuple[str, ...]
    domain_counts: Counter[str]
    range_counts: Counter[str]
    support: int
    avg_confidence: float

    def dominant_domain(self, min_share: float = 0.6) -> str | None:
        return _dominant(self.domain_counts, min_share)

    def dominant_range(self, min_share: float = 0.6) -> str | None:
        return _dominant(self.range_counts, min_share)


def induce_schema(
    edges: list[RelationArtifact],
    provider: ModelProvider,
    seeds: SeedPack,
    *,
    min_support: int = 3,
    role: ModelRole = ModelRole.PREDICATE_CANONICALIZER,
) -> tuple[RelationSchema, list[InducedPredicate]]:
    """Canonicalize predicate surfaces, aggregate types, return schema + audit."""
    if not edges:
        return RelationSchema(), []

    surfaces = sorted({e.predicate for e in edges})
    mapping = _canonicalize_predicates(surfaces, seeds, provider, role)

    grouped: dict[str, list[RelationArtifact]] = {}
    for edge in edges:
        canonical = mapping.get(edge.predicate, edge.predicate)
        grouped.setdefault(canonical, []).append(edge)

    induced: list[InducedPredicate] = []
    for canonical, group in grouped.items():
        if len(group) < min_support:
            continue
        d_counts: Counter[str] = Counter()
        r_counts: Counter[str] = Counter()
        for e in group:
            if e.subject_type:
                d_counts[e.subject_type] += 1
            if e.object_type:
                r_counts[e.object_type] += 1
        members = tuple(sorted(s for s, c in mapping.items() if c == canonical))
        induced.append(
            InducedPredicate(
                canonical=canonical,
                surfaces=members,
                domain_counts=d_counts,
                range_counts=r_counts,
                support=len(group),
                avg_confidence=sum(e.confidence for e in group) / len(group),
            )
        )

    schema = RelationSchema(
        types=tuple(
            RelationType(
                predicate=p.canonical,
                domain=p.dominant_domain() or "Thing",
                range=p.dominant_range() or "Thing",
            )
            for p in induced
        )
    )
    return schema, induced


def novel_promotion_candidates(
    induced: Iterable[InducedPredicate],
    seeds: SeedPack,
    *,
    min_support: int = 10,
    min_confidence: float = 0.7,
) -> list[RelationSeed]:
    """Predicates seen often enough to deserve a place in the next seed pack."""
    seed_names = {s.predicate for s in seeds.seeds}
    out: list[RelationSeed] = []
    for p in induced:
        if p.canonical in seed_names:
            continue
        if p.support < min_support or p.avg_confidence < min_confidence:
            continue
        domain = p.dominant_domain() or "?"
        range_ = p.dominant_range() or "?"
        out.append(
            RelationSeed(
                predicate=p.canonical,
                description=(
                    f"Auto-promoted from corpus: observed {p.support}× "
                    f"(typical {domain} → {range_})."
                ),
                preferred_domain=(domain,) if domain != "?" else (),
                preferred_range=(range_,) if range_ != "?" else (),
            )
        )
    return out


def _canonicalize_predicates(
    surfaces: list[str],
    seeds: SeedPack,
    provider: ModelProvider,
    role: ModelRole,
) -> dict[str, str]:
    if not surfaces:
        return {}
    seed_names = [s.predicate for s in seeds.seeds]
    prompt = (
        "Group the relation predicate surfaces into clusters of synonymous "
        "meaning. When a cluster matches a PREFERRED canonical name below, use "
        "that name. Otherwise pick a snake_case canonical.\n\n"
        f"Preferred canonical names:\n"
        + ("\n".join(f"- {n}" for n in seed_names) or "(none)")
        + "\n\nSurfaces:\n"
        + "\n".join(f"- {s}" for s in surfaces)
        + '\n\nReturn JSON: {"clusters": [{"canonical": str, "members": [str, ...]}]}'
    )
    response = llm_structured_transform(provider, role, prompt, CanonicalizationResponse)

    mapping: dict[str, str] = {}
    for cluster in response.clusters:
        canonical = cluster.canonical.strip().lower()
        if not canonical:
            continue
        for member in cluster.members:
            mapping[member] = canonical
    for surface in surfaces:
        mapping.setdefault(surface, surface)
    return mapping


def _dominant(counts: Counter[str], min_share: float) -> str | None:
    total = sum(counts.values())
    if total == 0:
        return None
    top, n = counts.most_common(1)[0]
    return top if n / total >= min_share else None


__all__ = [
    "InducedPredicate",
    "induce_schema",
    "novel_promotion_candidates",
]
