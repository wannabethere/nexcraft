"""Pydantic response models for LLM structured-output calls in the relations stack.

Used with :func:`ontology_foundry.llm.transform.llm_structured_transform` so the
provider response is validated and parsed in one place instead of ad-hoc JSON
handling inside each stage.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class RelationProposal(BaseModel):
    """One LLM-proposed edge between two spans in the chunk's span list."""

    model_config = ConfigDict(extra="ignore")

    subject_idx: int
    predicate: str
    object_idx: int
    confidence: float = 0.0
    evidence: str | None = None


class RelationResponse(BaseModel):
    model_config = ConfigDict(extra="ignore")

    relations: list[RelationProposal] = Field(default_factory=list)


class PredicateCluster(BaseModel):
    model_config = ConfigDict(extra="ignore")

    canonical: str
    members: list[str] = Field(default_factory=list)


class CanonicalizationResponse(BaseModel):
    model_config = ConfigDict(extra="ignore")

    clusters: list[PredicateCluster] = Field(default_factory=list)


__all__ = [
    "CanonicalizationResponse",
    "PredicateCluster",
    "RelationProposal",
    "RelationResponse",
]
