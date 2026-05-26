"""SQLAlchemy ORM models for the hierarchy.

Models match the spec tables in:
- T0_T1_organization_source_spec.md (Organization, OperatingRegion, Source)
- T0_T1_addendum_amundsenrds_linkage.md (source.cluster_rk linkage)
- T2_to_T6_amundsenrds_sidecar_spec.md (Catalog, SchemaExt, TableExt, ColumnExt, LineageEdge)
- mdl_table_concept_annotation_spec.md (table_ext annotation columns + AssetAnnotationProvenance)
- hierarchy_persistence_and_ingestion_spec.md (HierarchyAudit)

This file ships an internally-consistent minimal version of the amundsenrds spine
(`DatabaseMetadata`, `ClusterMetadata`, `SchemaMetadata`, `TableMetadata`, `ColumnMetadata`,
plus the description sidecar tables) so the pipeline can write end-to-end without
requiring the upstream `amundsen-rds` package as a dependency. When that package
is adopted, these tables are designed to be drop-in compatible.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import (
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    Interval,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from ontology_store.db.engine import Base

# ───────────────────────────────────────────────────────────────────────────
# T0 — Organization
# ───────────────────────────────────────────────────────────────────────────

class Organization(Base):
    __tablename__ = "organization"

    org_id: Mapped[str] = mapped_column(Text, primary_key=True)
    display_name: Mapped[str] = mapped_column(Text, nullable=False)
    legal_name: Mapped[str | None] = mapped_column(Text)
    industry: Mapped[str | None] = mapped_column(Text)
    sub_industry: Mapped[str | None] = mapped_column(Text)
    headquarters: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    primary_language: Mapped[str | None] = mapped_column(Text)
    supported_languages: Mapped[list[str] | None] = mapped_column(ARRAY(Text))
    locale_defaults: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    compliance_regimes: Mapped[list[str] | None] = mapped_column(ARRAY(Text))
    org_size_class: Mapped[str | None] = mapped_column(Text)
    business_context: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())


class OperatingRegion(Base):
    __tablename__ = "operating_region"

    org_id: Mapped[str] = mapped_column(ForeignKey("organization.org_id", ondelete="CASCADE"), primary_key=True)
    region_id: Mapped[str] = mapped_column(Text, primary_key=True)
    countries: Mapped[list[str]] = mapped_column(ARRAY(Text), nullable=False, default=list)
    languages: Mapped[list[str]] = mapped_column(ARRAY(Text), nullable=False, default=list)
    governance_profile: Mapped[str | None] = mapped_column(Text)
    locale_overrides: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    business_context: Mapped[str | None] = mapped_column(Text)


# ───────────────────────────────────────────────────────────────────────────
# Spine — amundsenrds-compatible minimal subset
# ───────────────────────────────────────────────────────────────────────────

class DatabaseMetadata(Base):
    """The platform kind ('postgres', 'snowflake', ...). amundsenrds-compatible."""
    __tablename__ = "database_metadata"

    rk: Mapped[str] = mapped_column(Text, primary_key=True)
    name: Mapped[str] = mapped_column(Text, nullable=False, unique=True)


class ClusterMetadata(Base):
    """A logical instance of a database. amundsenrds-compatible."""
    __tablename__ = "cluster_metadata"

    rk: Mapped[str] = mapped_column(Text, primary_key=True)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    database_rk: Mapped[str] = mapped_column(ForeignKey("database_metadata.rk", ondelete="CASCADE"), nullable=False)


class SchemaMetadata(Base):
    """A schema namespace inside a cluster. amundsenrds-compatible."""
    __tablename__ = "schema_metadata"

    rk: Mapped[str] = mapped_column(Text, primary_key=True)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    cluster_rk: Mapped[str] = mapped_column(ForeignKey("cluster_metadata.rk", ondelete="CASCADE"), nullable=False)


class TableMetadata(Base):
    """Table or view. amundsenrds-compatible."""
    __tablename__ = "table_metadata"

    rk: Mapped[str] = mapped_column(Text, primary_key=True)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    schema_rk: Mapped[str] = mapped_column(ForeignKey("schema_metadata.rk", ondelete="CASCADE"), nullable=False)
    is_view: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    __table_args__ = (
        Index("idx_table_metadata_schema_rk", "schema_rk"),
    )


class ColumnMetadata(Base):
    """Column. amundsenrds-compatible (a subset of fields)."""
    __tablename__ = "column_metadata"

    rk: Mapped[str] = mapped_column(Text, primary_key=True)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    table_rk: Mapped[str] = mapped_column(ForeignKey("table_metadata.rk", ondelete="CASCADE"), nullable=False)
    col_type: Mapped[str] = mapped_column(Text, nullable=False)
    sort_order: Mapped[int | None] = mapped_column(Integer)
    is_nullable: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)

    __table_args__ = (
        Index("idx_column_metadata_table_rk", "table_rk"),
    )


class TableDescription(Base):
    """User-authored table description (amundsenrds pattern)."""
    __tablename__ = "table_description"

    rk: Mapped[str] = mapped_column(Text, primary_key=True)
    table_rk: Mapped[str] = mapped_column(ForeignKey("table_metadata.rk", ondelete="CASCADE"), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False)
    source: Mapped[str] = mapped_column(Text, nullable=False, default="user")


class TableProgrammaticDescription(Base):
    """System-extracted table description; `source` carries the extractor id."""
    __tablename__ = "table_programmatic_description"

    rk: Mapped[str] = mapped_column(Text, primary_key=True)
    table_rk: Mapped[str] = mapped_column(ForeignKey("table_metadata.rk", ondelete="CASCADE"), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False)
    source: Mapped[str] = mapped_column(Text, nullable=False)


class ColumnDescription(Base):
    __tablename__ = "column_description"

    rk: Mapped[str] = mapped_column(Text, primary_key=True)
    column_rk: Mapped[str] = mapped_column(ForeignKey("column_metadata.rk", ondelete="CASCADE"), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False)
    source: Mapped[str] = mapped_column(Text, nullable=False, default="user")


class ColumnProgrammaticDescription(Base):
    __tablename__ = "column_programmatic_description"

    rk: Mapped[str] = mapped_column(Text, primary_key=True)
    column_rk: Mapped[str] = mapped_column(ForeignKey("column_metadata.rk", ondelete="CASCADE"), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False)
    source: Mapped[str] = mapped_column(Text, nullable=False)


# ───────────────────────────────────────────────────────────────────────────
# T1 — Source
# ───────────────────────────────────────────────────────────────────────────

class Source(Base):
    __tablename__ = "source"

    source_id: Mapped[str] = mapped_column(Text, primary_key=True)
    org_id: Mapped[str] = mapped_column(ForeignKey("organization.org_id", ondelete="CASCADE"), nullable=False)
    region_id: Mapped[str | None] = mapped_column(Text)
    kind: Mapped[str] = mapped_column(Text, nullable=False)
    vendor_details: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    instance_name: Mapped[str] = mapped_column(Text, nullable=False)
    display_name: Mapped[str] = mapped_column(Text, nullable=False)
    environment: Mapped[str] = mapped_column(Text, nullable=False, default="prod")
    role: Mapped[str] = mapped_column(Text, nullable=False, default="analytical_warehouse")
    purpose: Mapped[str | None] = mapped_column(Text)
    business_context: Mapped[str | None] = mapped_column(Text)
    business_owner: Mapped[str | None] = mapped_column(Text)
    technical_owner: Mapped[str | None] = mapped_column(Text)
    refresh_cadence: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    freshness_sla: Mapped[Any] = mapped_column(Interval, nullable=True)
    declared_residency: Mapped[list[str] | None] = mapped_column(ARRAY(Text))
    residency_check_mode: Mapped[str] = mapped_column(Text, nullable=False, default="best_effort")
    sensitivity_class: Mapped[str | None] = mapped_column(Text)
    pii_categories: Mapped[list[str] | None] = mapped_column(ARRAY(Text))
    auth_kind: Mapped[str | None] = mapped_column(Text)
    default_locale_overrides: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    notes: Mapped[str | None] = mapped_column(Text)
    # Linkage to amundsenrds spine (per T0/T1 addendum)
    cluster_rk: Mapped[str | None] = mapped_column(ForeignKey("cluster_metadata.rk", ondelete="RESTRICT"), unique=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())


# ───────────────────────────────────────────────────────────────────────────
# T2 — Catalog + schema_catalog sidecar
# ───────────────────────────────────────────────────────────────────────────

class Catalog(Base):
    __tablename__ = "catalog"

    catalog_uid: Mapped[str] = mapped_column(Text, primary_key=True)
    source_id: Mapped[str] = mapped_column(ForeignKey("source.source_id", ondelete="CASCADE"), nullable=False)
    catalog_name: Mapped[str] = mapped_column(Text, nullable=False)
    display_name: Mapped[str] = mapped_column(Text, nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    purpose: Mapped[str | None] = mapped_column(Text)
    lifecycle_stage: Mapped[str] = mapped_column(Text, nullable=False, default="production")
    access_pattern: Mapped[str] = mapped_column(Text, nullable=False, default="read_only")
    business_owner: Mapped[str | None] = mapped_column(Text)
    technical_owner: Mapped[str | None] = mapped_column(Text)
    sensitivity_class: Mapped[str | None] = mapped_column(Text)
    pii_categories: Mapped[list[str] | None] = mapped_column(ARRAY(Text))
    default_refresh_cadence: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    managed_by: Mapped[str | None] = mapped_column(Text)
    dbt_project_ref: Mapped[str | None] = mapped_column(Text)
    notes: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())

    __table_args__ = (
        UniqueConstraint("source_id", "catalog_name", name="uq_catalog_source_name"),
    )


class SchemaCatalog(Base):
    """Sidecar joining schema_metadata.rk → catalog.catalog_uid (1:N)."""
    __tablename__ = "schema_catalog"

    schema_rk: Mapped[str] = mapped_column(ForeignKey("schema_metadata.rk", ondelete="CASCADE"), primary_key=True)
    catalog_uid: Mapped[str] = mapped_column(ForeignKey("catalog.catalog_uid", ondelete="RESTRICT"), nullable=False)


# ───────────────────────────────────────────────────────────────────────────
# T3 — schema_ext
# ───────────────────────────────────────────────────────────────────────────

class SchemaExt(Base):
    __tablename__ = "schema_ext"

    schema_rk: Mapped[str] = mapped_column(ForeignKey("schema_metadata.rk", ondelete="CASCADE"), primary_key=True)
    display_name: Mapped[str] = mapped_column(Text, nullable=False)
    purpose: Mapped[str | None] = mapped_column(Text)
    domain_tags: Mapped[list[str]] = mapped_column(ARRAY(Text), nullable=False, default=list)
    lifecycle_stage: Mapped[str] = mapped_column(Text, nullable=False, default="production")
    business_owner: Mapped[str | None] = mapped_column(Text)
    technical_owner: Mapped[str | None] = mapped_column(Text)
    sensitivity_class: Mapped[str | None] = mapped_column(Text)
    pii_categories: Mapped[list[str] | None] = mapped_column(ARRAY(Text))
    managed_by: Mapped[str | None] = mapped_column(Text)
    notes: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())


# ───────────────────────────────────────────────────────────────────────────
# T4 — table_ext (with bottoms-up annotations)
# ───────────────────────────────────────────────────────────────────────────

class TableExt(Base):
    __tablename__ = "table_ext"

    table_rk: Mapped[str] = mapped_column(ForeignKey("table_metadata.rk", ondelete="CASCADE"), primary_key=True)
    display_name: Mapped[str] = mapped_column(Text, nullable=False)
    purpose: Mapped[str | None] = mapped_column(Text)
    lifecycle_stage: Mapped[str] = mapped_column(Text, nullable=False, default="production")
    is_materialized: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    materialization_kind: Mapped[str | None] = mapped_column(Text)
    view_definition: Mapped[str | None] = mapped_column(Text)
    view_depends_on: Mapped[list[str]] = mapped_column(ARRAY(Text), nullable=False, default=list)
    sensitivity_class: Mapped[str | None] = mapped_column(Text)
    pii_categories: Mapped[list[str] | None] = mapped_column(ARRAY(Text))
    data_product_member: Mapped[list[str]] = mapped_column(ARRAY(Text), nullable=False, default=list)
    # Bottoms-up annotations
    concepts: Mapped[list[str]] = mapped_column(ARRAY(Text), nullable=False, default=list)
    key_areas: Mapped[list[str]] = mapped_column(ARRAY(Text), nullable=False, default=list)
    causal_relations: Mapped[list[str]] = mapped_column(ARRAY(Text), nullable=False, default=list)
    notes: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())

    __table_args__ = (
        Index("idx_table_ext_concepts", "concepts", postgresql_using="gin"),
        Index("idx_table_ext_key_areas", "key_areas", postgresql_using="gin"),
        Index("idx_table_ext_causal_relations", "causal_relations", postgresql_using="gin"),
        Index("idx_table_ext_lifecycle_stage", "lifecycle_stage"),
    )


# ───────────────────────────────────────────────────────────────────────────
# T5 — column_ext
# ───────────────────────────────────────────────────────────────────────────

class ColumnExt(Base):
    __tablename__ = "column_ext"

    column_rk: Mapped[str] = mapped_column(ForeignKey("column_metadata.rk", ondelete="CASCADE"), primary_key=True)
    display_name: Mapped[str | None] = mapped_column(Text)
    purpose: Mapped[str | None] = mapped_column(Text)
    is_pii: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    pii_categories: Mapped[list[str]] = mapped_column(ARRAY(Text), nullable=False, default=list)
    is_business_key: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    semantic_unit: Mapped[str | None] = mapped_column(Text)
    sensitivity_class: Mapped[str | None] = mapped_column(Text)
    references_path: Mapped[str | None] = mapped_column(Text)  # 'schema.table.column' shape from MDL
    notes: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())


# ───────────────────────────────────────────────────────────────────────────
# Lineage
# ───────────────────────────────────────────────────────────────────────────

class LineageEdge(Base):
    __tablename__ = "lineage_edge"

    edge_id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    from_rk: Mapped[str] = mapped_column(Text, nullable=False)
    from_kind: Mapped[str] = mapped_column(Text, nullable=False)
    to_rk: Mapped[str] = mapped_column(Text, nullable=False)
    to_kind: Mapped[str] = mapped_column(Text, nullable=False)
    edge_kind: Mapped[str] = mapped_column(Text, nullable=False)
    evidence_kind: Mapped[str] = mapped_column(Text, nullable=False)
    evidence_ref: Mapped[str | None] = mapped_column(Text)
    confidence: Mapped[float | None] = mapped_column(Float)
    pipeline_ref: Mapped[str | None] = mapped_column(Text)
    # FK to relation_type.relation_type_pk — set by the relation-induction pass.
    # Null on creation; the post-pass updates it after `induce_schema` runs.
    predicate_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("relation_type.relation_type_pk", ondelete="SET NULL"),
        nullable=True,
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)

    __table_args__ = (
        UniqueConstraint(
            "from_rk", "from_kind", "to_rk", "to_kind", "edge_kind",
            name="uq_lineage_edge_path_kind",
        ),
        Index("idx_lineage_edge_from", "from_rk"),
        Index("idx_lineage_edge_to", "to_rk"),
    )


# ───────────────────────────────────────────────────────────────────────────
# Annotation provenance
# ───────────────────────────────────────────────────────────────────────────

class AssetAnnotationProvenance(Base):
    """Append-only history of every annotation write.

    Used by HierarchyDAO to enforce the no-clobber rule (don't overwrite
    human/service-authored annotations with LLM proposals).
    """
    __tablename__ = "asset_annotation_provenance"

    provenance_id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    asset_rk: Mapped[str] = mapped_column(Text, nullable=False)
    field: Mapped[str] = mapped_column(Text, nullable=False)  # 'concepts' | 'key_areas' | 'causal_relations'
    source: Mapped[str] = mapped_column(Text, nullable=False)  # 'llm_enrichment' | 'rule_*' | 'human'
    source_model: Mapped[str | None] = mapped_column(Text)
    confidence: Mapped[float | None] = mapped_column(Float)
    rationale: Mapped[str | None] = mapped_column(Text)
    written_by: Mapped[str] = mapped_column(Text, nullable=False)
    written_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())

    __table_args__ = (
        Index("idx_annotation_provenance_asset_field", "asset_rk", "field", "written_at"),
    )


# ───────────────────────────────────────────────────────────────────────────
# Audit
# ───────────────────────────────────────────────────────────────────────────

class HierarchyAudit(Base):
    __tablename__ = "hierarchy_audit"

    audit_id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    actor: Mapped[str] = mapped_column(Text, nullable=False)
    action: Mapped[str] = mapped_column(Text, nullable=False)  # 'create' | 'update' | 'append' | 'deactivate' | 'emit'
    tier: Mapped[str] = mapped_column(Text, nullable=False)    # 'T0' | 'T1' | ... | 'lineage' | 'binding'
    entity_uid: Mapped[str] = mapped_column(Text, nullable=False)
    field_path: Mapped[str | None] = mapped_column(Text)
    old_value: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    new_value: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    comment: Mapped[str | None] = mapped_column(Text)

    __table_args__ = (
        Index("idx_hierarchy_audit_entity", "tier", "entity_uid", "occurred_at"),
    )
