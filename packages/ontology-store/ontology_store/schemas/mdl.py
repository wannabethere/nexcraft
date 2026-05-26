"""MDL v2 wire format — shared between pipeline (writer) and retrieval (reader).

Mirrors the pipeline-internal MDL models so the store can accept what the
pipeline emits without translation. The pipeline's `models.GeneratedMDL` is
serialization-compatible with `MDLDocument` here.
"""
from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class MDLColumnProperties(BaseModel):
    model_config = ConfigDict(extra="allow")

    displayName: str | None = None
    description: str | None = None
    description_provenance: str | None = None
    is_primary_key: bool = False
    references: str | None = None  # 'schema.table.column'


class MDLColumn(BaseModel):
    name: str
    type: str
    notNull: bool = False
    rk: str
    properties: MDLColumnProperties = Field(default_factory=MDLColumnProperties)


class MDLMaterialization(BaseModel):
    kind: Literal["table", "view", "mv", "mv_incremental"] = "table"
    is_materialized: bool = False


class MDLViewDefinition(BaseModel):
    language: Literal["sql"] = "sql"
    query: str
    depends_on: list[str] = Field(default_factory=list)


class MDLModel(BaseModel):
    name: str
    rk: str
    description: str | None = None
    description_provenance: str | None = None
    is_view: bool = False
    tableReference: dict[str, str]
    materialization: MDLMaterialization
    view_definition: MDLViewDefinition | None = None
    columns: list[MDLColumn]
    # Bottoms-up annotations (may be present after annotation enrichment)
    concepts: list[str] = Field(default_factory=list)
    key_areas: list[str] = Field(default_factory=list)
    causal_relations: list[str] = Field(default_factory=list)


class MDLDocument(BaseModel):
    """The MDL v2 envelope — one document carries one or more models[]/endpoints[]/etc."""
    model_config = ConfigDict(populate_by_name=True)

    mdl_version: Literal["2.0"] = "2.0"
    source_id: str
    catalog: str | None = None
    schema_: str = Field(alias="schema")
    models: list[MDLModel] = Field(default_factory=list)
    endpoints: list[Any] = Field(default_factory=list)
    functions: list[Any] = Field(default_factory=list)
    metrics: list[Any] = Field(default_factory=list)
    streams: list[Any] = Field(default_factory=list)
