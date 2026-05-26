from __future__ import annotations

from enum import StrEnum
from typing import Any

from pydantic import AliasChoices, BaseModel, ConfigDict, Field


class Document(BaseModel):
    doc_id: str
    text: str
    metadata: dict[str, str] = Field(default_factory=dict)


class Entity(BaseModel):
    """Legacy flat entity; prefer :class:`EntitySpan` for NER pipeline output."""

    label: str
    text: str
    start: int
    end: int
    confidence: float | None = None
    source: str = "unknown"


class ChunkMetadata(BaseModel):
    """Per §3.5 / §11.1 — chunk header for NER and claim extraction."""

    model_config = ConfigDict(extra="allow")

    chunk_id: str
    parent_doc_id: str
    heading_path: str = ""
    prev_chunk_id: str | None = None
    next_chunk_id: str | None = None
    token_count: int = 0
    content_hash: str = ""


class DocumentChunk(BaseModel):
    """A single chunk of a parent document, ready for NER / claims."""

    metadata: ChunkMetadata
    text: str


class EntitySpan(BaseModel):
    """
    One typed span; matches `EntitySpanArtifact` items in
    `causal_ontology_foundry_design.md` (JSON field name \"type\").
    """

    model_config = ConfigDict(populate_by_name=True)

    text: str
    span_type: str = Field(
        validation_alias=AliasChoices("type", "span_type"),
        serialization_alias="type",
    )
    source_model: str = Field(
        validation_alias=AliasChoices("model", "source_model"),
        serialization_alias="model",
    )
    char_start: int
    char_end: int
    confidence: float
    seed_anchor: str | None = None
    extra: dict[str, Any] = Field(default_factory=dict)

    @classmethod
    def from_legacy_entity(cls, e: Entity) -> EntitySpan:
        return cls(
            text=e.text,
            span_type=e.label,
            source_model=e.source,
            char_start=e.start,
            char_end=e.end,
            confidence=e.confidence if e.confidence is not None else 0.0,
        )


class EntitySpanArtifact(BaseModel):
    """Per-chunk NER output (§3.6 NER Pipelines, foundry §4.1)."""

    chunk_id: str
    spans: list[EntitySpan] = Field(default_factory=list)


class ClaimType(StrEnum):
    DEFINITION = "definition"
    RULE = "rule"
    CAUSAL = "causal"
    GOVERNANCE = "governance"


class ClaimArtifact(BaseModel):
    """Structured claim with provenance (§3.7 / foundry §4.1)."""

    model_config = ConfigDict(extra="allow")

    claim_type: ClaimType
    text: str
    chunk_id: str
    confidence: float
    entity_refs: list[str] = Field(default_factory=list)
    supports_seed_id: str | None = None
    source: str = "claim_extractor"


class RetrievalHit(BaseModel):
    chunk_id: str
    content: str
    score: float
    metadata: dict[str, str] = Field(default_factory=dict)


class RelationArtifact(BaseModel):
    """A typed edge between two linked entities, with provenance.

    Parallels :class:`ClaimArtifact`: chunk-scoped, confidence-scored, source-tagged.
    `subject_ref` / `object_ref` are the linker's canonical anchors (typically
    `EntitySpan.seed_anchor`); using post-link references is what makes edges
    stable across mentions and chunks. `subject_type` / `object_type` carry the
    NER `span_type` so induction can aggregate domain/range without a separate
    span lookup table.
    """

    model_config = ConfigDict(extra="allow")

    subject_ref: str
    predicate: str
    object_ref: str
    subject_type: str = ""
    object_type: str = ""
    chunk_id: str
    confidence: float
    subject_span_idx: int | None = None
    object_span_idx: int | None = None
    source: str = "relation_extractor"
    evidence_text: str | None = None


class AnalysisResult(BaseModel):
    document_id: str
    entities: list[Entity] = Field(default_factory=list)
    retrieval_hits: list[RetrievalHit] = Field(default_factory=list)
    diagnostics: dict[str, str] = Field(default_factory=dict)
    span_artifacts: list[EntitySpanArtifact] = Field(default_factory=list)
    claims: list[ClaimArtifact] = Field(default_factory=list)
    relations: list[RelationArtifact] = Field(default_factory=list)
