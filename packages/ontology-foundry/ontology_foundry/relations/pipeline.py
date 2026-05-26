"""Compose relation stages and dedupe their output."""

from __future__ import annotations

from dataclasses import dataclass, field

from ontology_foundry.models import (
    ClaimArtifact,
    DocumentChunk,
    EntitySpanArtifact,
    RelationArtifact,
)
from ontology_foundry.relations.stages import RelationStage


@dataclass
class RelationPipeline:
    """Run each stage, merge their outputs, dedupe by (subject, predicate, object).

    Stages are expected to consume *linked* spans — typically the output of
    :class:`SeedFirstEntityLinker` applied to a chunk's :class:`EntitySpanArtifact`.
    """

    stages: tuple[RelationStage, ...] = field(default_factory=tuple)

    def extract_chunk(
        self,
        chunk: DocumentChunk,
        span_artifact: EntitySpanArtifact,
        claims: list[ClaimArtifact] | None = None,
    ) -> list[RelationArtifact]:
        edges: list[RelationArtifact] = []
        for stage in self.stages:
            edges.extend(stage.extract(chunk, span_artifact.spans, claims))
        return dedupe_keep_best(edges)


def dedupe_keep_best(edges: list[RelationArtifact]) -> list[RelationArtifact]:
    """Same (subject_ref, predicate, object_ref) triple from multiple stages →
    keep the highest-confidence proposal. Provenance is intentionally lost on
    losers — if union-of-evidence matters, swap this for a merging dedupe.
    """
    best: dict[tuple[str, str, str], RelationArtifact] = {}
    for edge in edges:
        key = (edge.subject_ref, edge.predicate, edge.object_ref)
        prev = best.get(key)
        if prev is None or edge.confidence > prev.confidence:
            best[key] = edge
    return list(best.values())


__all__ = ["RelationPipeline", "dedupe_keep_best"]
