"""Internal data shapes used across the pipeline.

These are NOT the MDL wire format directly — they're the in-memory
representation produced by introspection and consumed by MDL generation.
The MDL JSON envelope is rendered in `ontology_pipeline.mdl.generator`.
"""
from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


# ───────────────────────────────────────────────────────────────────────────
# Introspection result — what the source connector returns
# ───────────────────────────────────────────────────────────────────────────

class ColumnInfo(BaseModel):
    """One column observed in the source."""
    name: str
    sql_type: str
    nullable: bool
    default: str | None = None
    description: str | None = Field(
        default=None,
        description="Source-native COMMENT ON COLUMN value, if present.",
    )
    is_primary_key: bool = False
    references_table: str | None = Field(
        default=None,
        description="If this column is a declared FK, the qualified target table.",
    )
    references_column: str | None = None


class TableInfo(BaseModel):
    """One table observed in the source."""
    schema_name: str
    name: str
    description: str | None = Field(
        default=None,
        description="Source-native COMMENT ON TABLE, if present (rare in many DBs).",
    )
    columns: list[ColumnInfo]
    primary_key: list[str] = Field(default_factory=list)
    is_view: bool = False
    view_definition: str | None = None
    row_count_estimate: int | None = None

    @property
    def qualified_name(self) -> str:
        return f"{self.schema_name}.{self.name}"


class IntrospectionResult(BaseModel):
    """Full introspection of a source's relevant schemas."""
    source_id: str
    # 'postgres' = live DB via PostgresIntrospector.
    # 'local_files' = SqlFileIntrospector (preview mode; no live DB).
    source_kind: Literal["postgres", "local_files"]
    catalog: str | None  # database name for postgres; file stem for local_files
    extracted_at: datetime
    tables: list[TableInfo]

    def tables_by_qualified_name(self) -> dict[str, TableInfo]:
        return {t.qualified_name: t for t in self.tables}


# ───────────────────────────────────────────────────────────────────────────
# MDL output shape — minimal v2 envelope for one table
# ───────────────────────────────────────────────────────────────────────────

class MDLColumnProperties(BaseModel):
    """Column properties — extras allowed so enrichment stages can attach
    semantic_unit, is_pii, pii_categories, sensitivity_class, business_meaning, etc.
    without locking the schema. Downstream consumers read them via `.model_extra`."""
    model_config = ConfigDict(extra="allow")

    displayName: str | None = None
    description: str | None = None
    description_provenance: str | None = None
    is_primary_key: bool = False
    references: str | None = None  # "schema.table.column" for FKs


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
    """One asset entry within an MDL `models[]` array.

    `model_config = extra='allow'` lets enrichment stages attach additional
    blocks (e.g., `documentation` from RichDescriptionEnricher) without
    breaking the schema. Downstream consumers read them through model_dump().
    """
    model_config = ConfigDict(extra="allow")

    name: str
    rk: str
    description: str | None = None
    description_provenance: str | None = None
    is_view: bool = False
    tableReference: dict[str, str]
    materialization: MDLMaterialization
    view_definition: MDLViewDefinition | None = None
    columns: list[MDLColumn]
    # Bottoms-up annotations
    concepts: list[str] = Field(default_factory=list)
    key_areas: list[str] = Field(default_factory=list)
    causal_relations: list[str] = Field(default_factory=list)


class GeneratedMDL(BaseModel):
    """An MDL v2 document carrying one table's model entry.

    Matches the envelope from mdl_bundle_spec.md §3.1, with the model_kind-
    specific arrays. v1 pipeline emits one table per file.
    """
    mdl_version: Literal["2.0"] = "2.0"
    source_id: str
    catalog: str | None = None
    schema: str
    models: list[MDLModel] = Field(default_factory=list)
    endpoints: list = Field(default_factory=list)
    functions: list = Field(default_factory=list)
    metrics: list = Field(default_factory=list)
    streams: list = Field(default_factory=list)


# ───────────────────────────────────────────────────────────────────────────
# Annotations — concepts / key_areas / causal_relations + provenance
# ───────────────────────────────────────────────────────────────────────────

class AssetAnnotations(BaseModel):
    """Bottoms-up annotations for one asset.

    Mirrors what mdl_table_concept_annotation_spec.md §4 expects to land in
    `table_ext.{concepts,key_areas,causal_relations}` plus a provenance
    sidecar record.
    """
    asset_rk: str
    concepts: list[str] = Field(default_factory=list)
    key_areas: list[str] = Field(default_factory=list)
    causal_relations: list[str] = Field(default_factory=list)
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    rationale: str = ""
    source: str = "llm_enrichment"  # 'llm_enrichment' | 'rule_*' | 'human'
    source_model: str | None = None
    written_at: datetime = Field(default_factory=datetime.utcnow)


# ───────────────────────────────────────────────────────────────────────────
# Pipeline run accounting
# ───────────────────────────────────────────────────────────────────────────

class TableRunResult(BaseModel):
    """Per-table outcome of one pipeline run."""
    qualified_name: str
    asset_rk: str
    outcome: Literal["created", "updated", "unchanged", "error"]
    native_column_comments_preserved: int = 0
    column_descriptions_generated_by_llm: int = 0
    table_description_generated_by_llm: bool = False
    annotation_concepts_count: int = 0
    annotation_key_areas_count: int = 0
    annotation_causal_relations_count: int = 0
    llm_calls: int = 0
    wall_time_seconds: float = 0.0
    error: str | None = None


class PipelineRunResult(BaseModel):
    """Aggregate result from one pipeline run."""
    source_id: str
    started_at: datetime
    finished_at: datetime
    tables_seen: int
    tables_processed: int
    tables_skipped_unchanged: int
    tables_errored: int
    total_llm_calls: int
    per_table: list[TableRunResult]

    @property
    def wall_time_seconds(self) -> float:
        return (self.finished_at - self.started_at).total_seconds()
