"""Retrieval-side schemas — request scope + response shapes.

These mirror the contracts in retrieval_v2_spec.md so the retrieval API and
internal consumers can share types directly.
"""
from __future__ import annotations

from pydantic import BaseModel, Field


class RetrievalScope(BaseModel):
    """Filtering primitives. Replaces project_id (per retrieval_v2_spec)."""
    org_id: str
    source_ids: list[str] | None = None
    catalog_uids: list[str] | None = None
    schema_rks: list[str] | None = None
    concepts: list[str] | None = None
    key_areas: list[str] | None = None
    causal_relations: list[str] | None = None
    lifecycle_stages: list[str] | None = None
    include_deprecated: bool = False
    asset_kinds: list[str] | None = None
    sensitivity_max: str | None = None
    compliance_regimes: list[str] | None = None
    # Compat shim for callers migrating from the legacy project_id world.
    # The new pipeline ignores this field for routing; it travels with the scope
    # so audit / diagnostics can correlate. A future `legacy_project_translation`
    # table can map it into concepts/key_areas without changing call sites.
    legacy_project_id: str | None = None

    @classmethod
    def for_project(cls, project_id: str, *, org_id: str, **kwargs) -> "RetrievalScope":
        """Convenience constructor for legacy callers passing a project_id.

        Wires `legacy_project_id` while letting the caller add concepts/key_areas
        if known. Once the translation table is populated, this helper can
        resolve project_id → concepts/key_areas automatically.
        """
        return cls(org_id=org_id, legacy_project_id=project_id, **kwargs)


class AssetSearchFilters(BaseModel):
    """Sub-filter for asset search. Embedded into AssetHit responses."""
    concepts: list[str] | None = None
    key_areas: list[str] | None = None
    causal_relations: list[str] | None = None
    source_id_in: list[str] | None = None
    lifecycle_stage_in: list[str] | None = None
    asset_kind_in: list[str] | None = None
    effective_sensitivity_class_lte: str | None = None


class TableContextColumn(BaseModel):
    name: str
    type: str
    description: str | None = None
    description_provenance: str | None = None
    is_primary_key: bool = False
    is_pii: bool = False
    references_path: str | None = None


class TableContext(BaseModel):
    """A hydrated asset for retrieval consumers."""
    asset_rk: str
    asset_kind: str
    source_id: str
    catalog_uid: str | None = None
    schema_rk: str
    schema_name: str
    name: str
    description: str | None = None
    description_provenance: str | None = None
    concepts: list[str] = Field(default_factory=list)
    key_areas: list[str] = Field(default_factory=list)
    causal_relations: list[str] = Field(default_factory=list)
    lifecycle_stage: str = "production"
    effective_sensitivity_class: str | None = None
    columns: list[TableContextColumn] = Field(default_factory=list)
    score: float | None = None
    primary_object_type: str | None = None


class AssetHit(BaseModel):
    """A lightweight search-result entry."""
    asset_rk: str
    asset_kind: str
    name: str
    schema_name: str
    source_id: str
    score: float
    snippet: str | None = None
    concepts: list[str] = Field(default_factory=list)
    key_areas: list[str] = Field(default_factory=list)
    causal_relations: list[str] = Field(default_factory=list)
    lifecycle_stage: str = "production"
