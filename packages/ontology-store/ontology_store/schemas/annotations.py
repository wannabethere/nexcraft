"""Annotation schema — concepts / key_areas / causal_relations with provenance."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class AssetAnnotations(BaseModel):
    """Bottoms-up annotations for one asset.

    Matches what `ontology_pipeline.models.AssetAnnotations` produces; the store
    accepts this shape directly via `AnnotationDAO.write`.
    """
    model_config = ConfigDict(extra="ignore")

    asset_rk: str
    concepts: list[str] = Field(default_factory=list)
    key_areas: list[str] = Field(default_factory=list)
    causal_relations: list[str] = Field(default_factory=list)
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    rationale: str = ""
    source: Literal["llm_enrichment", "human"] | str = "llm_enrichment"
    source_model: str | None = None
    written_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    written_by: str = "system"
