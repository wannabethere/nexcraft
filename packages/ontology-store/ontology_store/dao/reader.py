"""AssetReader — read paths used by the retrieval service.

Wraps the SQL needed by the retrieval-side `BundleStore`-shaped operations.
For v1 there's no Qdrant integration — search is a Postgres ILIKE + array-
overlap filter. When Qdrant is added, `search_assets` gains a vector path.
"""
from __future__ import annotations

import logging
from typing import Any

from sqlalchemy import and_, func, or_, select
from sqlalchemy.dialects.postgresql import ARRAY
from sqlalchemy.orm import Session

from ontology_store.db.models import (
    ColumnDescription,
    ColumnExt,
    ColumnMetadata,
    ColumnProgrammaticDescription,
    SchemaCatalog,
    SchemaExt,
    SchemaMetadata,
    Source,
    TableDescription,
    TableExt,
    TableMetadata,
    TableProgrammaticDescription,
)
from ontology_store.db.stats_models import ColumnStat
from ontology_store.schemas import (
    AssetHit,
    FieldContext,
    RetrievalScope,
    TableContext,
    TableContextColumn,
)

logger = logging.getLogger(__name__)


class AssetReader:
    """Asset-side reads for the retrieval API."""

    def __init__(self, session: Session) -> None:
        self.s = session

    # ── single-asset lookup ─────────────────────────────────────────────

    def get_asset(self, asset_rk: str) -> TableContext | None:
        """Hydrate a single asset by rk: amundsenrds + sidecar + descriptions + columns."""
        stmt = (
            select(TableMetadata, TableExt, SchemaMetadata, SchemaCatalog)
            .join(TableExt, TableExt.table_rk == TableMetadata.rk, isouter=True)
            .join(SchemaMetadata, SchemaMetadata.rk == TableMetadata.schema_rk)
            .join(SchemaCatalog, SchemaCatalog.schema_rk == SchemaMetadata.rk, isouter=True)
            .where(TableMetadata.rk == asset_rk)
        )
        row = self.s.execute(stmt).first()
        if row is None:
            return None
        tbl, ext, schema, sc = row

        source_id = _source_id_from_cluster_rk(schema.cluster_rk)
        asset_kind = self._asset_kind_for(tbl, ext)

        # Descriptions: prefer user-authored over programmatic; capture provenance
        desc, prov = self._best_table_description(asset_rk)

        # Columns
        columns = self._fetch_columns_for_table(asset_rk)

        return TableContext(
            asset_rk=tbl.rk,
            asset_kind=asset_kind,
            source_id=source_id,
            catalog_uid=sc.catalog_uid if sc else None,
            schema_rk=schema.rk,
            schema_name=schema.name,
            name=tbl.name,
            description=desc,
            description_provenance=prov,
            concepts=(ext.concepts if ext else []) or [],
            key_areas=(ext.key_areas if ext else []) or [],
            causal_relations=(ext.causal_relations if ext else []) or [],
            lifecycle_stage=(ext.lifecycle_stage if ext else "production"),
            effective_sensitivity_class=(ext.sensitivity_class if ext else None),
            columns=columns,
            score=None,
            primary_object_type=(ext.concepts[0] if ext and ext.concepts else None),
        )

    # ── list + search ───────────────────────────────────────────────────

    def list_assets(
        self,
        *,
        scope: RetrievalScope,
        limit: int = 50,
        offset: int = 0,
    ) -> list[AssetHit]:
        """Filtered enumeration. No vector ranking; ordered by name."""
        stmt = self._base_select_for_scope(scope)
        stmt = stmt.order_by(TableMetadata.name).limit(limit).offset(offset)
        rows = self.s.execute(stmt).all()
        return [self._to_asset_hit(r, score=0.0) for r in rows]

    def search_assets(
        self,
        *,
        query: str,
        scope: RetrievalScope,
        k: int = 10,
    ) -> list[AssetHit]:
        """v1 search: SQL ILIKE on name + concept/key_area-aware ranking heuristic.

        When Qdrant is added, this method gains a vector-search path with
        payload filters and uses the SQL path as a fallback / hydration step.
        """
        q = (query or "").strip()
        like = f"%{q.lower()}%"

        stmt = self._base_select_for_scope(scope)
        if q:
            stmt = stmt.where(
                or_(
                    func.lower(TableMetadata.name).like(like),
                    func.lower(SchemaMetadata.name).like(like),
                )
            )
        stmt = stmt.limit(k * 4)  # over-fetch; rank below
        rows = self.s.execute(stmt).all()

        # Lightweight relevance: hit count of scope.concepts / scope.key_areas + name match.
        scoped_concepts = set(scope.concepts or [])
        scoped_key_areas = set(scope.key_areas or [])
        scoped_causal = set(scope.causal_relations or [])

        scored: list[tuple[float, Any]] = []
        for r in rows:
            tbl, ext, schema, sc = r
            score = 0.0
            if q and (q.lower() in tbl.name.lower() or q.lower() in schema.name.lower()):
                score += 1.0
            if ext is not None:
                if scoped_concepts:
                    score += 1.5 * len(scoped_concepts.intersection(ext.concepts or []))
                if scoped_key_areas:
                    score += 1.0 * len(scoped_key_areas.intersection(ext.key_areas or []))
                if scoped_causal:
                    score += 1.0 * len(scoped_causal.intersection(ext.causal_relations or []))
            scored.append((score, r))

        scored.sort(key=lambda pair: pair[0], reverse=True)
        top = scored[:k]
        return [self._to_asset_hit(r, score=s) for s, r in top if s > 0 or not q]

    # ── private helpers ─────────────────────────────────────────────────

    def _base_select_for_scope(self, scope: RetrievalScope):
        stmt = (
            select(TableMetadata, TableExt, SchemaMetadata, SchemaCatalog)
            .join(TableExt, TableExt.table_rk == TableMetadata.rk, isouter=True)
            .join(SchemaMetadata, SchemaMetadata.rk == TableMetadata.schema_rk)
            .join(SchemaCatalog, SchemaCatalog.schema_rk == SchemaMetadata.rk, isouter=True)
        )

        # source_id filtering — derive from cluster_rk prefix when scope.source_ids set
        if scope.source_ids:
            cluster_prefixes = [
                # cluster_rk pattern: '{kind}://{source_id}' or '{kind}://{source_id}.{catalog}'
                f"%://{sid}" for sid in scope.source_ids
            ] + [
                f"%://{sid}.%" for sid in scope.source_ids
            ]
            stmt = stmt.where(
                or_(*[SchemaMetadata.cluster_rk.like(p) for p in cluster_prefixes])
            )

        if scope.schema_rks:
            stmt = stmt.where(SchemaMetadata.rk.in_(scope.schema_rks))

        if scope.catalog_uids:
            stmt = stmt.where(SchemaCatalog.catalog_uid.in_(scope.catalog_uids))

        # Annotation filters on table_ext
        if scope.concepts:
            stmt = stmt.where(TableExt.concepts.overlap(scope.concepts))
        if scope.key_areas:
            stmt = stmt.where(TableExt.key_areas.overlap(scope.key_areas))
        if scope.causal_relations:
            stmt = stmt.where(TableExt.causal_relations.overlap(scope.causal_relations))

        if scope.lifecycle_stages:
            stmt = stmt.where(TableExt.lifecycle_stage.in_(scope.lifecycle_stages))
        elif not scope.include_deprecated:
            stmt = stmt.where(
                or_(TableExt.lifecycle_stage.is_(None),
                    TableExt.lifecycle_stage != "deprecated")
            )

        # asset_kind filter — derived (table vs view); we accept the asset_kind values
        if scope.asset_kinds:
            kinds = set(scope.asset_kinds)
            if "table" in kinds and "view" not in kinds:
                stmt = stmt.where(TableMetadata.is_view.is_(False))
            elif "view" in kinds and "table" not in kinds:
                stmt = stmt.where(TableMetadata.is_view.is_(True))
            # If both or neither relevant, no filter needed.

        return stmt

    def _to_asset_hit(self, row, *, score: float) -> AssetHit:
        tbl, ext, schema, sc = row
        return AssetHit(
            asset_rk=tbl.rk,
            asset_kind=self._asset_kind_for(tbl, ext),
            name=tbl.name,
            schema_name=schema.name,
            source_id=_source_id_from_cluster_rk(schema.cluster_rk),
            score=score,
            concepts=(ext.concepts if ext else []) or [],
            key_areas=(ext.key_areas if ext else []) or [],
            causal_relations=(ext.causal_relations if ext else []) or [],
            lifecycle_stage=(ext.lifecycle_stage if ext else "production"),
        )

    @staticmethod
    def _asset_kind_for(tbl, ext) -> str:
        if not tbl.is_view:
            return "table"
        if ext and ext.is_materialized:
            return "materialized_view"
        return "view"

    def _best_table_description(self, asset_rk: str) -> tuple[str | None, str | None]:
        """Prefer user-authored over programmatic; report provenance string."""
        # User-authored first
        stmt = select(TableDescription).where(TableDescription.table_rk == asset_rk).limit(1)
        row = self.s.execute(stmt).scalar_one_or_none()
        if row is not None:
            return row.description, row.source
        stmt = (
            select(TableProgrammaticDescription)
            .where(TableProgrammaticDescription.table_rk == asset_rk)
            .limit(1)
        )
        row = self.s.execute(stmt).scalar_one_or_none()
        if row is not None:
            return row.description, row.source
        return None, None

    def _fetch_columns_for_table(self, asset_rk: str) -> list[TableContextColumn]:
        stmt = (
            select(ColumnMetadata, ColumnExt)
            .join(ColumnExt, ColumnExt.column_rk == ColumnMetadata.rk, isouter=True)
            .where(ColumnMetadata.table_rk == asset_rk)
            .order_by(ColumnMetadata.sort_order, ColumnMetadata.name)
        )
        rows = self.s.execute(stmt).all()
        out: list[TableContextColumn] = []
        for col, ext in rows:
            desc, prov = self._best_column_description(col.rk)
            out.append(TableContextColumn(
                name=col.name,
                type=col.col_type,
                description=desc,
                description_provenance=prov,
                is_primary_key=(ext.is_business_key if ext else False),
                is_pii=(ext.is_pii if ext else False),
                references_path=(ext.references_path if ext else None),
            ))
        return out

    def _best_column_description(self, column_rk: str) -> tuple[str | None, str | None]:
        stmt = select(ColumnDescription).where(ColumnDescription.column_rk == column_rk).limit(1)
        row = self.s.execute(stmt).scalar_one_or_none()
        if row is not None:
            return row.description, row.source
        stmt = (
            select(ColumnProgrammaticDescription)
            .where(ColumnProgrammaticDescription.column_rk == column_rk)
            .limit(1)
        )
        row = self.s.execute(stmt).scalar_one_or_none()
        if row is not None:
            return row.description, row.source
        return None, None


class FieldReader:
    """Field-side (T5) reads for the reindex worker.

    Symmetric to :class:`AssetReader`. Hydrates a single column_rk into a
    :class:`FieldContext` by joining column_metadata + column_ext +
    best_description + column_stat + parent breadcrumb (table → schema →
    cluster → source). The reindex worker calls :py:meth:`get_field` per
    queued ``qdrant_field`` task.
    """

    def __init__(self, session: Session) -> None:
        self.s = session

    def get_field(self, column_rk: str) -> FieldContext | None:
        """Hydrate a column by rk. Returns None if the column was deleted."""
        stmt = (
            select(ColumnMetadata, ColumnExt, TableMetadata, SchemaMetadata)
            .join(ColumnExt, ColumnExt.column_rk == ColumnMetadata.rk, isouter=True)
            .join(TableMetadata, TableMetadata.rk == ColumnMetadata.table_rk)
            .join(SchemaMetadata, SchemaMetadata.rk == TableMetadata.schema_rk)
            .where(ColumnMetadata.rk == column_rk)
        )
        row = self.s.execute(stmt).first()
        if row is None:
            return None
        col, ext, tbl, schema = row

        desc, prov = self._best_column_description(column_rk)
        stat = self.s.execute(
            select(ColumnStat).where(ColumnStat.column_rk == column_rk).limit(1),
        ).scalar_one_or_none()
        source_id = _source_id_from_cluster_rk(schema.cluster_rk)

        return FieldContext(
            column_rk=col.rk,
            name=col.name,
            col_type=col.col_type,
            is_nullable=col.is_nullable,
            sort_order=col.sort_order,
            description=desc,
            description_provenance=prov,
            display_name=(ext.display_name if ext else None),
            purpose=(ext.purpose if ext else None),
            is_pii=(ext.is_pii if ext else False),
            pii_categories=list(ext.pii_categories) if ext and ext.pii_categories else [],
            is_business_key=(ext.is_business_key if ext else False),
            semantic_unit=(ext.semantic_unit if ext else None),
            sensitivity_class=(ext.sensitivity_class if ext else None),
            references_path=(ext.references_path if ext else None),
            cardinality_tier=(stat.cardinality_tier if stat else None),
            null_rate=(stat.null_rate if stat else None),
            distinct_count=(stat.distinct_count if stat else None),
            parent_rk=tbl.rk,
            parent_name=tbl.name,
            schema_rk=schema.rk,
            schema_name=schema.name,
            source_id=source_id,
            field_kind="column",
        )

    def _best_column_description(self, column_rk: str) -> tuple[str | None, str | None]:
        """Same provenance rule as AssetReader: user → programmatic → None."""
        stmt = select(ColumnDescription).where(ColumnDescription.column_rk == column_rk).limit(1)
        row = self.s.execute(stmt).scalar_one_or_none()
        if row is not None:
            return row.description, row.source
        stmt = (
            select(ColumnProgrammaticDescription)
            .where(ColumnProgrammaticDescription.column_rk == column_rk)
            .limit(1)
        )
        row = self.s.execute(stmt).scalar_one_or_none()
        if row is not None:
            return row.description, row.source
        return None, None


def _source_id_from_cluster_rk(cluster_rk: str) -> str:
    """Derive source_id from cluster_rk = '{kind}://{source_id}' or '{kind}://{source_id}.{catalog}'."""
    if "://" not in cluster_rk:
        return cluster_rk
    after = cluster_rk.split("://", 1)[1]
    return after.split(".", 1)[0]
