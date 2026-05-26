"""Relation extraction stages: produce typed edges from linked spans + claims.

The default `SeededLlmRelationStage` is domain-agnostic: it takes a
:class:`SeedPack` as a soft vocabulary hint and lets the LLM propose novel
predicates when the seeds don't fit. The :class:`StubRelationStage` is a
deterministic test stub — useful as a regression baseline and for running the
pipeline without an LLM in CI.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol

from ontology_foundry.llm.provider import ModelProvider, ModelRole
from ontology_foundry.llm.transform import llm_structured_transform
from ontology_foundry.models import (
    ClaimArtifact,
    DocumentChunk,
    EntitySpan,
    RelationArtifact,
)
from ontology_foundry.relations.artifacts import RelationResponse
from ontology_foundry.relations.seeds import SeedPack


class RelationStage(Protocol):
    name: str

    def extract(
        self,
        chunk: DocumentChunk,
        spans: list[EntitySpan],
        claims: list[ClaimArtifact] | None = None,
    ) -> list[RelationArtifact]:
        ...


def _ref(span: EntitySpan) -> str:
    """Prefer the linker's anchor; fall back to a stable surface-keyed local IRI."""
    if span.seed_anchor:
        return span.seed_anchor
    surface = span.text.strip().replace(" ", "_") or "blank"
    return f"local:{span.span_type}/{surface}"


def _normalize_predicate(p: str) -> str:
    return "_".join(p.strip().lower().split())


@dataclass
class SeededLlmRelationStage:
    """LLM relation extraction biased by a seed vocabulary.

    Seeds are *hints*: predicates in the seed pack are listed in the prompt with
    descriptions; the LLM is asked to prefer them but may propose novel
    predicates. Each emitted artifact carries `source` ending in `:seeded` or
    `:novel` so reviewers can audit novelty downstream.
    """

    provider: ModelProvider
    seeds: SeedPack
    name: str = "seeded-llm-relations"
    min_confidence: float = 0.5
    allow_novel: bool = True
    role: ModelRole = ModelRole.RELATION_EXTRACTOR

    def extract(
        self,
        chunk: DocumentChunk,
        spans: list[EntitySpan],
        claims: list[ClaimArtifact] | None = None,
    ) -> list[RelationArtifact]:
        if len(spans) < 2:
            return []

        prompt = self._build_prompt(chunk.text, spans, claims or [])
        response = llm_structured_transform(self.provider, self.role, prompt, RelationResponse)

        seed_names = {s.predicate for s in self.seeds.seeds}
        out: list[RelationArtifact] = []
        for proposal in response.relations:
            predicate = _normalize_predicate(proposal.predicate)
            if not predicate or proposal.confidence < self.min_confidence:
                continue
            i, j = proposal.subject_idx, proposal.object_idx
            if i == j or not (0 <= i < len(spans) and 0 <= j < len(spans)):
                continue
            is_novel = predicate not in seed_names
            if is_novel and not self.allow_novel:
                continue
            subj, obj = spans[i], spans[j]
            out.append(
                RelationArtifact(
                    subject_ref=_ref(subj),
                    predicate=predicate,
                    object_ref=_ref(obj),
                    subject_type=subj.span_type,
                    object_type=obj.span_type,
                    chunk_id=chunk.metadata.chunk_id,
                    confidence=proposal.confidence,
                    subject_span_idx=i,
                    object_span_idx=j,
                    source=f"{self.name}:{'novel' if is_novel else 'seeded'}",
                    evidence_text=proposal.evidence,
                )
            )
        return out

    def _build_prompt(
        self,
        text: str,
        spans: list[EntitySpan],
        claims: list[ClaimArtifact],
    ) -> str:
        seed_block = (
            "\n".join(
                f"- {s.predicate}: {s.description}"
                + (f" (e.g. {s.examples[0]})" if s.examples else "")
                for s in self.seeds.seeds
            )
            or "(no seed vocabulary supplied)"
        )
        span_block = "\n".join(
            f"[{i}] {s.span_type}: {s.text!r}"
            + (f" (anchor={s.seed_anchor})" if s.seed_anchor else "")
            for i, s in enumerate(spans)
        )
        claim_block = (
            "\n".join(f"- ({c.claim_type}) {c.text}" for c in claims) or "(none)"
        )
        return (
            "Extract typed relations between the entities below.\n\n"
            "PREFERRED predicates (use these exact snake_case names when applicable):\n"
            f"{seed_block}\n\n"
            "If no preferred predicate fits, you may invent a snake_case predicate "
            "name. Only emit a relation if the text or a claim directly supports it. "
            "Use 0-based indices from the entity list as subject_idx / object_idx.\n\n"
            'Return JSON: {"relations": [{"subject_idx": int, "predicate": str, '
            '"object_idx": int, "confidence": float, "evidence": str}]}\n\n'
            f"Entities:\n{span_block}\n\n"
            f"Claims:\n{claim_block}\n\n"
            f"Text:\n{text}"
        )


@dataclass
class StubRelationStage:
    """Deterministic stage for tests, regression baselines, and offline pipelines.

    Emits a fixed set of edges by `(subject_span_idx, predicate, object_span_idx,
    confidence)` tuples scoped per chunk_id. Useful as a regression floor: if
    your LLM stage's recall drops below this, something's wrong upstream."""

    edges_by_chunk: dict[str, tuple[tuple[int, str, int, float], ...]] = field(
        default_factory=dict
    )
    name: str = "stub-relations"

    def extract(
        self,
        chunk: DocumentChunk,
        spans: list[EntitySpan],
        claims: list[ClaimArtifact] | None = None,
    ) -> list[RelationArtifact]:
        recipes = self.edges_by_chunk.get(chunk.metadata.chunk_id, ())
        out: list[RelationArtifact] = []
        for subj_idx, predicate, obj_idx, conf in recipes:
            if not (0 <= subj_idx < len(spans) and 0 <= obj_idx < len(spans)):
                continue
            if subj_idx == obj_idx:
                continue
            subj, obj = spans[subj_idx], spans[obj_idx]
            out.append(
                RelationArtifact(
                    subject_ref=_ref(subj),
                    predicate=_normalize_predicate(predicate),
                    object_ref=_ref(obj),
                    subject_type=subj.span_type,
                    object_type=obj.span_type,
                    chunk_id=chunk.metadata.chunk_id,
                    confidence=conf,
                    subject_span_idx=subj_idx,
                    object_span_idx=obj_idx,
                    source=self.name,
                )
            )
        return out


__all__ = ["RelationStage", "SeededLlmRelationStage", "StubRelationStage"]
