"""HierarchyDAO — write paths for the spine + extensions.

Two principal entry points:

- `upsert_org_source_catalog(...)` — bootstrap a tenant's identity records
  (T0 / T1 / T2). Idempotent — safe to call repeatedly.

- `upsert_mdl_document(...)` — write one MDL document's data into the spine:
    1. Ensure `database_metadata` row exists for the source kind.
    2. Ensure `cluster_metadata` row exists; link to `source.cluster_rk`.
    3. Ensure `schema_metadata` row; link to `catalog` via `schema_catalog`.
    4. Upsert `schema_ext` (display_name, defaults).
    5. For each model in the MDL: upsert `table_metadata` + `table_ext`
       (including concepts/key_areas/causal_relations if present) +
       description rows.
    6. For each column: upsert `column_metadata` + `column_ext` + description rows.
    7. For each declared FK: upsert `lineage_edge` with `edge_kind='depends_on'`.

Audit rows are written for every meaningful mutation. The no-clobber rule for
annotations is enforced in `AnnotationDAO`, not here — `upsert_mdl_document`
deliberately does NOT write annotations unless they are present on the MDL and
no prior LLM-prior annotation exists (a fresh ingest case).
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from ontology_store.db.models import (
    Catalog,
    ClusterMetadata,
    ColumnDescription,
    ColumnExt,
    ColumnMetadata,
    ColumnProgrammaticDescription,
    DatabaseMetadata,
    HierarchyAudit,
    LineageEdge,
    Organization,
    SchemaCatalog,
    SchemaExt,
    SchemaMetadata,
    Source,
    TableDescription,
    TableExt,
    TableMetadata,
    TableProgrammaticDescription,
)
from ontology_store.schemas import (
    CatalogIn,
    MDLDocument,
    MDLModel,
    OrganizationIn,
    SourceIn,
)

logger = logging.getLogger(__name__)


class HierarchyDAO:
    """Write paths for spine + extensions. Caller manages the session."""

    def __init__(self, session: Session, *, actor: str = "system") -> None:
        self.s = session
        self.actor = actor

    # ── T0 / T1 / T2 bootstrap ──────────────────────────────────────────

    def upsert_organization(self, org: OrganizationIn) -> Organization:
        existing = self.s.get(Organization, org.org_id)
        payload = org.model_dump(exclude_unset=False)
        if existing is None:
            row = Organization(**payload)
            self.s.add(row)
            self.s.flush()  # flush now (session is autoflush=False) so same-session get() sees it
            self._audit("create", "T0", org.org_id, new_value=payload)
            return row
        # Update in place
        for k, v in payload.items():
            setattr(existing, k, v)
        existing.updated_at = datetime.now(timezone.utc)
        self._audit("update", "T0", org.org_id, new_value=payload)
        return existing

    def upsert_source(self, source: SourceIn, *, cluster_rk: str | None = None) -> Source:
        existing = self.s.get(Source, source.source_id)
        payload = source.model_dump(exclude_unset=False)
        if cluster_rk is not None:
            payload["cluster_rk"] = cluster_rk
        if existing is None:
            row = Source(**payload)
            self.s.add(row)
            self.s.flush()  # flush now (session is autoflush=False) so upsert_mdl_document's get(Source) sees it
            self._audit("create", "T1", source.source_id, new_value=payload)
            return row
        for k, v in payload.items():
            setattr(existing, k, v)
        existing.updated_at = datetime.now(timezone.utc)
        self._audit("update", "T1", source.source_id, new_value=payload)
        return existing

    def upsert_catalog(self, catalog: CatalogIn) -> Catalog:
        uid = catalog.catalog_uid
        existing = self.s.get(Catalog, uid)
        payload = catalog.model_dump(exclude={"catalog_uid"})
        payload["catalog_uid"] = uid
        if payload.get("display_name") is None:
            payload["display_name"] = catalog.catalog_name
        if existing is None:
            row = Catalog(**payload)
            self.s.add(row)
            self._audit("create", "T2", uid, new_value=payload)
            return row
        for k, v in payload.items():
            setattr(existing, k, v)
        existing.updated_at = datetime.now(timezone.utc)
        self._audit("update", "T2", uid, new_value=payload)
        return existing

    # ── Spine ensure helpers (idempotent) ───────────────────────────────

    def ensure_database_metadata(self, kind: str) -> DatabaseMetadata:
        row = self.s.get(DatabaseMetadata, kind)
        if row is None:
            row = DatabaseMetadata(rk=kind, name=kind)
            self.s.add(row)
            self.s.flush()
        return row

    def ensure_cluster_metadata(
        self, *, source_id: str, kind: str, catalog: str | None
    ) -> ClusterMetadata:
        # Cluster rk derives from source + (optional) catalog
        rk = f"{kind}://{source_id}" + (f".{catalog}" if catalog else "")
        row = self.s.get(ClusterMetadata, rk)
        if row is None:
            self.ensure_database_metadata(kind)
            row = ClusterMetadata(rk=rk, name=source_id, database_rk=kind)
            self.s.add(row)
            self.s.flush()
        return row

    def ensure_schema_metadata(
        self, *, cluster_rk: str, schema_name: str
    ) -> SchemaMetadata:
        rk = f"{cluster_rk}/{schema_name}"
        row = self.s.get(SchemaMetadata, rk)
        if row is None:
            row = SchemaMetadata(rk=rk, name=schema_name, cluster_rk=cluster_rk)
            self.s.add(row)
            self.s.flush()
        return row

    def ensure_schema_catalog_link(self, *, schema_rk: str, catalog_uid: str) -> None:
        existing = self.s.get(SchemaCatalog, schema_rk)
        if existing is None:
            self.s.add(SchemaCatalog(schema_rk=schema_rk, catalog_uid=catalog_uid))
            self.s.flush()
        elif existing.catalog_uid != catalog_uid:
            existing.catalog_uid = catalog_uid

    def upsert_schema_ext(self, *, schema_rk: str, display_name: str, **kwargs: object) -> SchemaExt:
        existing = self.s.get(SchemaExt, schema_rk)
        if existing is None:
            existing = SchemaExt(schema_rk=schema_rk, display_name=display_name, **kwargs)  # type: ignore[arg-type]
            self.s.add(existing)
            self._audit("create", "T3", schema_rk, new_value={"display_name": display_name})
        else:
            existing.display_name = display_name
            for k, v in kwargs.items():
                setattr(existing, k, v)
            existing.updated_at = datetime.now(timezone.utc)
            self._audit("update", "T3", schema_rk)
        return existing

    # ── MDL → spine + extensions ────────────────────────────────────────

    def upsert_mdl_document(
        self, doc: MDLDocument, *,
        source_id: str | None = None,
        write_annotations_inline: bool = True,
    ) -> list[str]:
        """Write an MDL document into the spine + extensions.

        Returns the list of asset_rks touched. Source must exist (call
        `upsert_source` first). When `write_annotations_inline=True`, the
        MDLModel's concepts/key_areas/causal_relations are copied to
        `table_ext` directly — but only when no prior provenance row exists
        (no-clobber); use `AnnotationDAO.write` for the typical post-MDL
        annotation pass.
        """
        sid = source_id or doc.source_id
        source = self.s.get(Source, sid)
        if source is None:
            raise ValueError(
                f"Source {sid!r} not found. Call upsert_source(...) first."
            )

        # Ensure spine rows exist for this MDL's location.
        cluster = self.ensure_cluster_metadata(
            source_id=sid, kind=source.kind, catalog=doc.catalog,
        )
        # Link source to cluster if not already
        if source.cluster_rk is None:
            source.cluster_rk = cluster.rk

        schema = self.ensure_schema_metadata(cluster_rk=cluster.rk, schema_name=doc.schema_)

        # Catalog binding (optional — only when catalog is set on the MDL)
        if doc.catalog is not None:
            catalog_uid = f"{sid}::catalog::{doc.catalog}"
            # If catalog row missing, create a minimal one (display_name = catalog_name).
            if self.s.get(Catalog, catalog_uid) is None:
                self.upsert_catalog(CatalogIn(source_id=sid, catalog_name=doc.catalog))
            self.ensure_schema_catalog_link(schema_rk=schema.rk, catalog_uid=catalog_uid)

        # schema_ext minimum (display = schema name)
        self.upsert_schema_ext(schema_rk=schema.rk, display_name=doc.schema_)

        touched: list[str] = []
        for model in doc.models:
            self._upsert_model(model=model, schema_rk=schema.rk, write_annotations_inline=write_annotations_inline)
            touched.append(model.rk)
        return touched

    # ── Per-model write ─────────────────────────────────────────────────

    def _upsert_model(
        self, *, model: MDLModel, schema_rk: str, write_annotations_inline: bool
    ) -> None:
        # Table metadata
        tbl = self.s.get(TableMetadata, model.rk)
        if tbl is None:
            tbl = TableMetadata(
                rk=model.rk, name=model.name, schema_rk=schema_rk, is_view=model.is_view,
            )
            self.s.add(tbl)
            self._audit("create", "T4", model.rk)
        else:
            tbl.name = model.name
            tbl.schema_rk = schema_rk
            tbl.is_view = model.is_view
            self._audit("update", "T4", model.rk)
        # Persist table_metadata before its dependents (table_ext, column_metadata,
        # *_description, lineage_edge) — session is autoflush=False.
        self.s.flush()

        # Enqueue Qdrant reindex for this asset.
        # Importing lazily to avoid a hard runtime dep when the workers module
        # isn't used (DAO consumers without the vector extra still work).
        try:
            from ontology_store.workers.queue import enqueue_asset_reindex
            enqueue_asset_reindex(
                self.s, asset_rk=model.rk,
                asset_kind=("view" if model.is_view else "table"),
            )
        except Exception as exc:
            logger.debug("Skipping reindex enqueue for %s: %s", model.rk, exc)

        # Descriptions — write into the correct table by provenance
        if model.description:
            self._upsert_description(
                asset_rk=model.rk,
                description=model.description,
                source=model.description_provenance or "user",
                target="table",
            )

        # table_ext — never clobber existing annotation arrays unless asked
        self._upsert_table_ext(model=model, write_annotations_inline=write_annotations_inline)

        # Columns
        for col in model.columns:
            self._upsert_column(col=col, table_rk=model.rk)

        # FK lineage from columns' references property
        for col in model.columns:
            ref = col.properties.references
            if not ref:
                continue
            # ref shape: "schema.table.column"
            parts = ref.split(".")
            if len(parts) >= 2:
                # We can build a target table rk only if we share the schema -> table mapping;
                # for v1 we record the raw reference path in column_ext + a lineage edge
                # whose to_rk is best-effort (same cluster + ref schema/table).
                target_table = ".".join(parts[:2])  # schema.table
                # Build target rk: same cluster as this table
                target_rk = self._cluster_prefix_of(schema_rk) + "/" + target_table.replace(".", "/")
                self._upsert_lineage_edge(
                    from_rk=model.rk,
                    to_rk=target_rk,
                    edge_kind="depends_on",
                    evidence_kind="declared_fk",
                )

    def _upsert_table_ext(self, *, model: MDLModel, write_annotations_inline: bool) -> None:
        existing = self.s.get(TableExt, model.rk)
        kwargs = dict(
            display_name=_display_from_name(model.name),
            lifecycle_stage="production",
            is_materialized=model.materialization.is_materialized,
            materialization_kind=model.materialization.kind,
            view_definition=(model.view_definition.query if model.view_definition else None),
            view_depends_on=(model.view_definition.depends_on if model.view_definition else []),
        )
        if existing is None:
            new = TableExt(table_rk=model.rk, **kwargs)  # type: ignore[arg-type]
            # Annotations may be present from the pipeline; safe to copy on create.
            if write_annotations_inline:
                new.concepts = model.concepts or []
                new.key_areas = model.key_areas or []
                new.causal_relations = model.causal_relations or []
            self.s.add(new)
        else:
            for k, v in kwargs.items():
                setattr(existing, k, v)
            existing.updated_at = datetime.now(timezone.utc)
            # Do NOT clobber annotations here; AnnotationDAO enforces no-clobber.

    def _upsert_column(self, *, col: object, table_rk: str) -> None:
        # col is MDLColumn; typed loosely to avoid circular imports
        rk = col.rk  # type: ignore[attr-defined]
        existing = self.s.get(ColumnMetadata, rk)
        if existing is None:
            existing = ColumnMetadata(
                rk=rk,
                name=col.name,  # type: ignore[attr-defined]
                table_rk=table_rk,
                col_type=col.type,  # type: ignore[attr-defined]
                is_nullable=not col.notNull,  # type: ignore[attr-defined]
            )
            self.s.add(existing)
        else:
            existing.name = col.name  # type: ignore[attr-defined]
            existing.col_type = col.type  # type: ignore[attr-defined]
            existing.is_nullable = not col.notNull  # type: ignore[attr-defined]
        # Persist column_metadata NOW so the column_ext / *_description FKs below
        # resolve (session is autoflush=False; otherwise the column_ext insertmany
        # can run before column_metadata and violate column_ext_column_rk_fkey).
        self.s.flush()

        props = col.properties  # type: ignore[attr-defined]

        # Description (provenance-aware)
        if props.description:
            self._upsert_description(
                asset_rk=rk,
                description=props.description,
                source=props.description_provenance or "user",
                target="column",
            )

        # column_ext
        ext_existing = self.s.get(ColumnExt, rk)
        kwargs = dict(
            display_name=props.displayName,
            is_business_key=props.is_primary_key,
            references_path=props.references,
        )
        if ext_existing is None:
            self.s.add(ColumnExt(column_rk=rk, **kwargs))  # type: ignore[arg-type]
        else:
            for k, v in kwargs.items():
                setattr(ext_existing, k, v)
            ext_existing.updated_at = datetime.now(timezone.utc)

        # Enqueue Qdrant reindex for this column (T5). Same lazy-import
        # pattern as the asset enqueue above — DAO consumers without the
        # workers extra still work.
        try:
            from ontology_store.workers.queue import enqueue_field_reindex
            enqueue_field_reindex(self.s, column_rk=rk, parent_rk=table_rk)
        except Exception as exc:
            logger.debug("Skipping field reindex enqueue for %s: %s", rk, exc)

    def _upsert_description(self, *, asset_rk: str, description: str, source: str, target: str) -> None:
        """Idempotent write to {table|column}_description / _programmatic_description by source."""
        rk = f"{asset_rk}::desc::{source}"
        is_programmatic = source != "user"
        if target == "table":
            cls = TableProgrammaticDescription if is_programmatic else TableDescription
            existing = self.s.get(cls, rk)  # type: ignore[arg-type]
            if existing is None:
                row = cls(rk=rk, table_rk=asset_rk, description=description, source=source)
                self.s.add(row)
            else:
                existing.description = description  # type: ignore[attr-defined]
        else:
            cls = ColumnProgrammaticDescription if is_programmatic else ColumnDescription
            existing = self.s.get(cls, rk)  # type: ignore[arg-type]
            if existing is None:
                row = cls(rk=rk, column_rk=asset_rk, description=description, source=source)
                self.s.add(row)
            else:
                existing.description = description  # type: ignore[attr-defined]

    def _upsert_lineage_edge(
        self, *, from_rk: str, to_rk: str, edge_kind: str, evidence_kind: str,
        confidence: float | None = None, evidence_ref: str | None = None,
    ) -> None:
        stmt = (
            select(LineageEdge)
            .where(
                LineageEdge.from_rk == from_rk,
                LineageEdge.to_rk == to_rk,
                LineageEdge.edge_kind == edge_kind,
            )
        )
        existing = self.s.execute(stmt).scalar_one_or_none()
        if existing is None:
            self.s.add(LineageEdge(
                from_rk=from_rk, from_kind="table",
                to_rk=to_rk, to_kind="table",
                edge_kind=edge_kind, evidence_kind=evidence_kind,
                confidence=confidence, evidence_ref=evidence_ref,
                active=True,
            ))
            # flush now (session is autoflush=False) so a repeat edge later in THIS
            # session — e.g. two columns of the same table referencing the same target —
            # is found by the existence check above instead of duplicate-inserting
            # (uq_lineage_edge_path_kind).
            self.s.flush()
        else:
            existing.active = True
            existing.confidence = confidence if confidence is not None else existing.confidence

    # ── Helpers ─────────────────────────────────────────────────────────

    @staticmethod
    def _cluster_prefix_of(schema_rk: str) -> str:
        """Reconstruct the cluster_rk from a schema_rk (drops trailing /schema_name)."""
        return schema_rk.rsplit("/", 1)[0]

    def _audit(
        self, action: str, tier: str, entity_uid: str, *,
        field_path: str | None = None,
        new_value: dict | None = None,
        old_value: dict | None = None,
    ) -> None:
        self.s.add(HierarchyAudit(
            actor=self.actor,
            action=action,
            tier=tier,
            entity_uid=entity_uid,
            field_path=field_path,
            old_value=old_value,
            new_value=_json_safe(new_value) if new_value else None,
        ))


def _display_from_name(name: str) -> str:
    return " ".join(p.capitalize() for p in name.split("_") if p)


def _json_safe(value: object) -> object:
    """Best-effort JSON-safe coercion for audit values."""
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, dict):
        return {k: _json_safe(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_json_safe(v) for v in value]
    return value
