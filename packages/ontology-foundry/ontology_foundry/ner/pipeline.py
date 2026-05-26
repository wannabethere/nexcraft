from __future__ import annotations

from dataclasses import dataclass, field

from ontology_foundry.models import DocumentChunk, EntitySpan, EntitySpanArtifact
from ontology_foundry.ner.merge import merge_entity_spans
from ontology_foundry.ner.stages import (
    CapitalizedFallbackStage,
    CausalMarkerStage,
    GlinerNerStage,
    SpacyNerStage,
)


@dataclass
class HybridNerConfig:
    """Hybrid stack from §3.6 / foundry §4.1 (spaCy → GLiNER → rules → merge)."""

    spacy_model: str = "en_core_web_sm"
    gliner_model: str = "urchade/gliner_medium-v2.1"
    ner_labels: tuple[str, ...] = (
        "entity_name",
        "attribute",
        "concept",
        "event",
        "actor_role",
        "policy_reference",
        "quantitative_claim",
        "temporal_qualifier",
    )
    use_capitalized_fallback: bool = True


@dataclass
class HybridNerPipeline:
    """
    Sequential stages (DAG edges). Parallel backend execution is handled by the
    ingestion executor; this class composes results and merges overlaps.
    """

    config: HybridNerConfig = field(default_factory=HybridNerConfig)
    spacy_stage: SpacyNerStage | None = None
    fallback_stage: CapitalizedFallbackStage | None = None
    gliner_stage: GlinerNerStage | None = None
    rules_stage: CausalMarkerStage | None = None

    def __post_init__(self) -> None:
        self.spacy_stage = self.spacy_stage or SpacyNerStage(self.config.spacy_model)
        self.fallback_stage = self.fallback_stage or CapitalizedFallbackStage()
        self.gliner_stage = self.gliner_stage or GlinerNerStage(
            model_name=self.config.gliner_model,
            ner_labels=self.config.ner_labels,
        )
        self.rules_stage = self.rules_stage or CausalMarkerStage()

    def extract_chunk(self, chunk: DocumentChunk) -> EntitySpanArtifact:
        text = chunk.text
        spans: list[EntitySpan] = []
        spacy_spans = self.spacy_stage.extract(text) if self.spacy_stage else []
        if (
            not spacy_spans
            and self.config.use_capitalized_fallback
            and self.fallback_stage is not None
        ):
            spacy_spans = self.fallback_stage.extract(text)
        spans.extend(spacy_spans)
        if self.gliner_stage is not None:
            spans.extend(self.gliner_stage.extract(text))
        if self.rules_stage is not None:
            spans.extend(self.rules_stage.extract(text))
        merged = merge_entity_spans(spans)
        return EntitySpanArtifact(chunk_id=chunk.metadata.chunk_id, spans=merged)
