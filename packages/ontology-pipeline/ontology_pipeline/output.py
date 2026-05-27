"""Output sink for pipeline results.

v1: filesystem only. Writes one MDL JSON per table + one annotations JSON per
table to the configured output directory. Future: HierarchyStore-backed sink
that writes to Postgres + Qdrant (per hierarchy_persistence_and_ingestion_spec).
"""
from __future__ import annotations

import json
import logging
import os
import tempfile
from pathlib import Path
from typing import Any, Protocol

from ontology_pipeline.config import OutputConfig
from ontology_pipeline.models import AssetAnnotations, GeneratedMDL

logger = logging.getLogger(__name__)


class Sink(Protocol):
    """Pipeline output destination.

    Core writes (`write_mdl`, `write_annotations`) are required. Side-output
    writes (`write_inferred_relationships`, `write_data_protection_hints`,
    `write_causal_candidates`) are optional; implementations that don't support
    them may no-op. The orchestrator calls them when an enrichment stage
    produced corresponding side_output.
    """

    def write_mdl(self, *, source_id: str, schema: str, table: str, mdl: GeneratedMDL) -> Path: ...
    def write_annotations(
        self, *, source_id: str, schema: str, table: str, annotations: AssetAnnotations,
    ) -> Path: ...
    def base_dir(self) -> Path: ...

    def write_inferred_relationships(
        self,
        *,
        source_id: str,
        schema: str,
        table: str,
        from_table_rk: str,
        items: list[dict[str, Any]],
    ) -> None: ...
    """Per-asset inferred FKs (each item: from_column, to_table_qualified,
    to_column, confidence, cardinality_hint, reason). The sink resolves
    `to_table_qualified` ('schema.table') into a full rk for storage."""

    def write_data_protection_hints(
        self,
        *,
        source_id: str,
        schema: str,
        table: str,
        asset_rk: str,
        hints: dict[str, Any],
    ) -> None: ...
    """Asset-level RLS / CLS suggestions. hints = {rls_predicates, cls_columns,
    rationale, provenance}."""

    def write_causal_candidates(
        self,
        *,
        source_id: str,
        schema: str,
        table: str,
        candidates: list[dict[str, Any]],
    ) -> None: ...
    """Proposed causal edges. Each candidate has: asset_rk, subject_ref,
    predicate, object_ref, evidence_columns, mechanism_hint, confidence,
    status, provenance."""

    def write_table_aggregates(
        self,
        *,
        source_id: str,
        schema: str,
        table: str,
        table_rk: str,
        aggregates: list[Any],
        population_row_count: int | None = None,
        source_system: str | None = None,
    ) -> None: ...
    """Phase 1 of stats persistence: scalar per-column aggregates (n_rows,
    null_rate, distinct_count, min/max/mean/stddev, cardinality_tier).
    Always safe to call — these are shape facts, not values. Items are
    `ColumnAggregate` dataclasses (`ontology_store.dao.stats`)."""

    def write_table_samples(
        self,
        *,
        source_id: str,
        schema: str,
        table: str,
        table_facts: Any,
        column_top_frequencies: dict[str, list[dict[str, Any]]],
        pii_safe_column_rks: set[str],
    ) -> None: ...
    """Phase 2 of stats persistence: value-bearing fields (row samples + top-k).
    Caller passes the set of column_rks that the data_protection enricher
    cleared. Sinks gate writes by this set. `table_facts` is a
    `TableSampleFacts` (`ontology_store.dao.stats`)."""

    def write_relation_schema(
        self,
        *,
        source_id: str,
        types: list[Any],
        attachments: list[dict[str, Any]],
    ) -> None: ...
    """Run-once-per-pipeline write of the induced predicate TBox.

    Args:
        types: list of `RelationTypeIn` dataclasses (one per
            (predicate, domain, range) triple from `induce_schema`).
        attachments: list of `{from_rk, to_rk, edge_kind, predicate, domain,
            range_type}` — for each row, the sink looks up the just-upserted
            `relation_type` row and sets the contributing `lineage_edge.predicate_id`.
    """


class FilesystemSink:
    """Writes MDL + annotations to a filesystem tree.

    Layout:
        <base_dir>/mdl/<source_id>/<schema>/<table>.json
        <base_dir>/annotations/<source_id>/<schema>/<table>.annotations.json
    """

    def __init__(self, cfg: OutputConfig) -> None:
        self._base = Path(cfg.base_dir).resolve()
        self._base.mkdir(parents=True, exist_ok=True)

    def base_dir(self) -> Path:
        return self._base

    def write_mdl(self, *, source_id: str, schema: str, table: str, mdl: GeneratedMDL) -> Path:
        target = self._base / "mdl" / source_id / schema / f"{table}.json"
        target.parent.mkdir(parents=True, exist_ok=True)
        _atomic_write_json(target, mdl.model_dump(mode="json", exclude_none=True))
        return target

    def write_annotations(
        self, *, source_id: str, schema: str, table: str, annotations: AssetAnnotations,
    ) -> Path:
        target = self._base / "annotations" / source_id / schema / f"{table}.annotations.json"
        target.parent.mkdir(parents=True, exist_ok=True)
        _atomic_write_json(target, annotations.model_dump(mode="json", exclude_none=True))
        return target

    def write_inferred_relationships(
        self, *, source_id: str, schema: str, table: str,
        from_table_rk: str, items: list[dict[str, Any]],
    ) -> None:
        if not items:
            return
        target = (
            self._base / "inferred_relationships" / source_id / schema / f"{table}.json"
        )
        _atomic_write_json(target, {
            "from_table_rk": from_table_rk,
            "items": items,
        })

    def write_data_protection_hints(
        self, *, source_id: str, schema: str, table: str,
        asset_rk: str, hints: dict[str, Any],
    ) -> None:
        if not hints:
            return
        target = (
            self._base / "data_protection_hints" / source_id / schema / f"{table}.json"
        )
        _atomic_write_json(target, {
            "asset_rk": asset_rk,
            **hints,
        })

    def write_causal_candidates(
        self, *, source_id: str, schema: str, table: str,
        candidates: list[dict[str, Any]],
    ) -> None:
        if not candidates:
            return
        target = (
            self._base / "causal_candidates" / source_id / schema / f"{table}.json"
        )
        _atomic_write_json(target, {"candidates": candidates})

    def write_table_aggregates(
        self, *, source_id: str, schema: str, table: str,
        table_rk: str, aggregates: list[Any],
        population_row_count: int | None = None,
        source_system: str | None = None,
    ) -> None:
        if not aggregates:
            return
        target = (
            self._base / "column_stats" / source_id / schema / f"{table}.aggregates.json"
        )
        _atomic_write_json(target, {
            "table_rk": table_rk,
            "population_row_count": population_row_count,
            "source_system": source_system,
            "columns": [_dataclass_to_dict(a) for a in aggregates],
        })

    def write_table_samples(
        self, *, source_id: str, schema: str, table: str,
        table_facts: Any, column_top_frequencies: dict[str, list[dict[str, Any]]],
        pii_safe_column_rks: set[str],
    ) -> None:
        gated_freqs = {
            rk: freqs for rk, freqs in column_top_frequencies.items()
            if rk in pii_safe_column_rks
        }
        if not gated_freqs and not getattr(table_facts, "sample_rows", None):
            return
        target = (
            self._base / "column_stats" / source_id / schema / f"{table}.samples.json"
        )
        _atomic_write_json(target, {
            "table_rk": getattr(table_facts, "table_rk", None),
            "sample_rows": list(getattr(table_facts, "sample_rows", []) or []),
            "sample_row_count": getattr(table_facts, "sample_row_count", None),
            "top_frequencies": gated_freqs,
            "pii_safe_columns": sorted(pii_safe_column_rks),
        })

    def write_relation_schema(
        self, *, source_id: str,
        types: list[Any], attachments: list[dict[str, Any]],
    ) -> None:
        if not types and not attachments:
            return
        target = self._base / "relation_schema" / source_id / "relation_schema.json"
        _atomic_write_json(target, {
            "types": [_dataclass_to_dict(t) for t in types],
            "attachments": attachments,
        })


def _dataclass_to_dict(obj: Any) -> dict[str, Any]:
    """Best-effort dataclass → dict. Falls back to vars() for non-dataclasses."""
    try:
        from dataclasses import asdict, is_dataclass
        if is_dataclass(obj):
            return asdict(obj)
    except Exception:
        pass
    return dict(vars(obj))


def _atomic_write_json(target: Path, data: dict) -> None:
    """Write JSON to target atomically via temp-file + rename."""
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(
        prefix=f".{target.name}.",
        suffix=".tmp",
        dir=str(target.parent),
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=2, sort_keys=False, default=str)
            fh.write("\n")
        os.replace(tmp_path, target)
    except Exception:
        # Clean up on failure
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


class HierarchyStoreSink:
    """Writes MDL + annotations into the ontology-store Postgres tables via DAOs.

    Use when you want the pipeline's outputs landed in the shared spine
    (amundsenrds-compatible models + sidecars + annotations + lineage edges)
    rather than (or in addition to) flat-file emission. Used jointly with
    `FilesystemSink` via `TeeSink` when both are desired.

    Connection: pass an ontology_store.Database OR set ONTOLOGY_STORE_URL.
    """

    def __init__(
        self,
        *,
        db: "Any | None" = None,
        org_id: str,
        actor: str = "system",
        ensure_org_source: bool = True,
        env_var: str = "ONTOLOGY_STORE_URL",
    ) -> None:
        # Lazy imports so callers without ontology-store can still use FilesystemSink.
        from ontology_store import Database  # type: ignore[import]
        self._Database = Database
        if db is None:
            db = Database.from_env(env_var)
        self._db = db
        self._org_id = org_id
        self._actor = actor
        self._ensure_org_source = ensure_org_source
        self._seen_sources: set[str] = set()

    def base_dir(self) -> Path:
        # No filesystem layout — return a synthetic anchor under cwd for run-state.
        anchor = Path.cwd() / ".ontology_pipeline_state"
        anchor.mkdir(parents=True, exist_ok=True)
        return anchor

    def write_mdl(self, *, source_id: str, schema: str, table: str, mdl: GeneratedMDL) -> Path:
        from ontology_store import HierarchyDAO, MDLDocument as StoreMDLDocument  # type: ignore[import]
        from ontology_store.schemas import OrganizationIn, SourceIn  # type: ignore[import]

        # Translate the pipeline's GeneratedMDL → store's MDLDocument by JSON round-trip.
        # Both are Pydantic; the wire format matches (the store schema uses alias="schema"
        # so we serialize then validate with the alias-respecting model).
        as_json = mdl.model_dump(mode="json", exclude_none=True)
        store_doc = StoreMDLDocument.model_validate(as_json)

        with self._db.session() as session:
            dao = HierarchyDAO(session, actor=self._actor)
            _ensured_source = False
            if self._ensure_org_source and source_id not in self._seen_sources:
                # Ensure the Organization + Source rows exist; idempotent.
                dao.upsert_organization(OrganizationIn(
                    org_id=self._org_id, display_name=self._org_id,
                ))
                dao.upsert_source(SourceIn(
                    source_id=source_id,
                    org_id=self._org_id,
                    kind="postgres",
                    instance_name=source_id,
                    display_name=source_id,
                ))
                _ensured_source = True
            dao.upsert_mdl_document(store_doc)
        # Mark the source seen only AFTER the session commits — otherwise a failed
        # write_mdl would roll back the source insert but leave it flagged, and every
        # later table would skip the upsert and hit "Source not found".
        if _ensured_source:
            self._seen_sources.add(source_id)

        # No file is written; return a synthetic path for the return-type contract.
        synth = self.base_dir() / "mdl" / source_id / schema / f"{table}.json.virtual"
        synth.parent.mkdir(parents=True, exist_ok=True)
        synth.write_text(
            f"# Virtual marker — actual data lives in ontology-store table_metadata + table_ext\n"
            f"# asset_rk: {mdl.models[0].rk if mdl.models else '?'}\n",
            encoding="utf-8",
        )
        return synth

    def write_annotations(
        self, *, source_id: str, schema: str, table: str, annotations: AssetAnnotations,
    ) -> Path:
        from ontology_store import AnnotationDAO  # type: ignore[import]
        from ontology_store.schemas import AssetAnnotations as StoreAssetAnnotations  # type: ignore[import]

        as_json = annotations.model_dump(mode="json", exclude_none=True)
        store_anno = StoreAssetAnnotations.model_validate(as_json)

        with self._db.session() as session:
            dao = AnnotationDAO(session, actor=self._actor)
            outcomes = dao.write(store_anno)

        synth = self.base_dir() / "annotations" / source_id / schema / f"{table}.annotations.json.virtual"
        synth.parent.mkdir(parents=True, exist_ok=True)
        synth.write_text(
            f"# Virtual marker — annotations landed in table_ext + asset_annotation_provenance\n"
            f"# asset_rk: {annotations.asset_rk}\n"
            f"# outcomes: {outcomes}\n",
            encoding="utf-8",
        )
        return synth

    def write_inferred_relationships(
        self, *, source_id: str, schema: str, table: str,
        from_table_rk: str, items: list[dict[str, Any]],
    ) -> None:
        """Inferred FKs → lineage_edge rows (evidence_kind='inferred_relationship')."""
        if not items:
            return
        from ontology_store import InferenceDAO  # type: ignore[import]

        # Resolve to_table_qualified ('schema.table') → full rk using the from_table_rk shape.
        # from_table_rk = '<scheme>://<source>.<catalog>/<schema>/<table>'
        # Drop trailing '/<schema>/<table>' to get the cluster prefix.
        cluster_prefix = from_table_rk.rsplit("/", 2)[0] if "/" in from_table_rk else from_table_rk
        normalized: list[dict[str, Any]] = []
        for item in items:
            to_table = item.get("to_table_qualified") or ""
            if not to_table:
                continue
            to_rk = cluster_prefix + "/" + to_table.replace(".", "/")
            normalized.append({
                "from_rk": item.get("from_table_rk", from_table_rk),
                "to_rk": to_rk,
                "confidence": item.get("confidence"),
                "reason": item.get("reason"),
                "cardinality_hint": item.get("cardinality_hint") or "",
            })
        if not normalized:
            return
        with self._db.session() as session:
            counts = InferenceDAO(session, actor=self._actor).upsert_inferred_relationships(
                items=normalized,
            )
        logger.info(
            "HierarchyStoreSink: lineage_edge inferred for %s.%s.%s — inserted=%d updated=%d",
            source_id, schema, table, counts["inserted"], counts["updated"],
        )

    def write_data_protection_hints(
        self, *, source_id: str, schema: str, table: str,
        asset_rk: str, hints: dict[str, Any],
    ) -> None:
        if not hints:
            return
        from ontology_store import InferenceDAO  # type: ignore[import]
        from ontology_store.workers.queue import QueueDAO, TaskKind  # type: ignore[import]
        with self._db.session() as session:
            row = InferenceDAO(session, actor=self._actor).upsert_data_protection_hint(
                asset_rk=asset_rk,
                rls_predicates=hints.get("rls_predicates") or [],
                cls_columns=hints.get("cls_columns") or [],
                rationale=hints.get("rationale") or "",
                provenance=hints.get("provenance") or "llm_data_protection",
            )
            session.flush()
            if row is not None:
                QueueDAO(session).enqueue(
                    task_kind=TaskKind.EVENT_PROTECTION,
                    payload={
                        "tenant_id": self._org_id,
                        "row_id": row.hint_id,
                        "org_id": self._org_id,
                    },
                )
        logger.info("HierarchyStoreSink: data_protection_hint upserted for %s", asset_rk)

    def write_causal_candidates(
        self, *, source_id: str, schema: str, table: str,
        candidates: list[dict[str, Any]],
    ) -> None:
        if not candidates:
            return
        from ontology_store import InferenceDAO  # type: ignore[import]
        from ontology_store.db.inference_models import CausalCandidate  # type: ignore[import]
        from ontology_store.workers.queue import QueueDAO, TaskKind  # type: ignore[import]
        from sqlalchemy import select  # type: ignore[import]

        with self._db.session() as session:
            dao = InferenceDAO(session, actor=self._actor)
            counts = dao.upsert_causal_candidates(items=candidates)
            session.flush()
            # Emit one EVENT_CAUSAL task per just-written row so the
            # reindex worker appends an event to CAUSAL_EVENTS.
            qdao = QueueDAO(session)
            for c in candidates:
                row = session.execute(
                    select(CausalCandidate).where(
                        CausalCandidate.asset_rk == c["asset_rk"],
                        CausalCandidate.subject_ref == c["subject_ref"],
                        CausalCandidate.predicate == c["predicate"],
                        CausalCandidate.object_ref == c["object_ref"],
                    )
                ).scalar_one_or_none()
                if row is None:
                    continue
                qdao.enqueue(
                    task_kind=TaskKind.EVENT_CAUSAL,
                    payload={
                        "tenant_id": self._org_id,
                        "row_id": row.candidate_id,
                        "org_id": self._org_id,
                        "source_id": source_id,
                    },
                )
        logger.info(
            "HierarchyStoreSink: causal_candidate writes for %s.%s.%s — inserted=%d updated=%d",
            source_id, schema, table, counts["inserted"], counts["updated"],
        )

    def write_table_aggregates(
        self, *, source_id: str, schema: str, table: str,
        table_rk: str, aggregates: list[Any],
        population_row_count: int | None = None,
        source_system: str | None = None,
    ) -> None:
        if not aggregates:
            return
        from ontology_store.dao.stats import ColumnStatDAO  # type: ignore[import]
        with self._db.session() as session:
            counts = ColumnStatDAO(session, actor=self._actor).upsert_aggregates(
                table_rk=table_rk,
                aggregates=aggregates,
                population_row_count=population_row_count,
                source_system=source_system,
            )
        logger.info(
            "HierarchyStoreSink: column_stat aggregates for %s.%s.%s — inserted=%d updated=%d",
            source_id, schema, table, counts["inserted"], counts["updated"],
        )

    def write_table_samples(
        self, *, source_id: str, schema: str, table: str,
        table_facts: Any, column_top_frequencies: dict[str, list[dict[str, Any]]],
        pii_safe_column_rks: set[str],
    ) -> None:
        from ontology_store.dao.stats import ColumnStatDAO  # type: ignore[import]
        with self._db.session() as session:
            counts = ColumnStatDAO(session, actor=self._actor).attach_sampled_values(
                table_facts=table_facts,
                column_top_frequencies=column_top_frequencies,
                gate=lambda col_rk: col_rk in pii_safe_column_rks,
            )
        logger.info(
            "HierarchyStoreSink: column_stat samples for %s.%s.%s — promoted=%d blocked=%d row_sample=%d",
            source_id, schema, table,
            counts["columns_promoted"], counts["columns_blocked"], counts["row_sample_persisted"],
        )

    def write_relation_schema(
        self, *, source_id: str,
        types: list[Any], attachments: list[dict[str, Any]],
    ) -> None:
        """Persist the induced TBox + link each contributing lineage_edge.

        Two-phase within one session: upsert all `relation_type` rows first
        so they all have ids, then walk `attachments` and update
        `lineage_edge.predicate_id`. Each TBox row + each attachment emits
        an event to RELATION_EVENTS via the reindex queue.
        """
        if not types and not attachments:
            return
        from ontology_store.dao import RelationTypeDAO  # type: ignore[import]
        from ontology_store.workers.queue import QueueDAO, TaskKind  # type: ignore[import]
        with self._db.session() as session:
            dao = RelationTypeDAO(session, actor=self._actor)
            tx_counts = dao.upsert_many(org_id=self._org_id, specs=types)
            # Need a flush so predicate_id is available for the attach pass.
            session.flush()

            qdao = QueueDAO(session)

            # Emit one RELATION_TYPE_CANONICALIZED event per upserted row.
            for spec in types:
                rt = dao.get_relation_type(
                    org_id=self._org_id,
                    predicate=spec.predicate,
                    domain=spec.domain,
                    range_type=spec.range_type,
                )
                if rt is None:
                    continue
                qdao.enqueue(
                    task_kind=TaskKind.EVENT_RELATION,
                    payload={
                        "tenant_id": self._org_id,
                        "row_id": rt.relation_type_pk,
                        "org_id": self._org_id,
                    },
                )

            attach_items: list[dict[str, Any]] = []
            for a in attachments:
                rt = dao.get_relation_type(
                    org_id=self._org_id,
                    predicate=a["predicate"],
                    domain=a["domain"],
                    range_type=a["range_type"],
                )
                if rt is None:
                    continue
                attach_items.append({
                    "from_rk": a["from_rk"],
                    "to_rk": a["to_rk"],
                    "edge_kind": a["edge_kind"],
                    "predicate_id": rt.relation_type_pk,
                })
            attach_counts = dao.attach_many(items=attach_items)
        logger.info(
            "HierarchyStoreSink: relation_type writes — types_inserted=%d types_updated=%d "
            "edges_attached=%d edges_missing=%d",
            tx_counts["inserted"], tx_counts["updated"],
            attach_counts["attached"], attach_counts["missing"],
        )


class TeeSink:
    """Composite sink that writes through multiple sinks in order.

    Useful for running FilesystemSink + HierarchyStoreSink together so outputs
    land both on disk (for inspection / debugging) and in the store (for queries).
    """

    def __init__(self, sinks: list[Sink]) -> None:
        if not sinks:
            raise ValueError("TeeSink requires at least one sink")
        self._sinks = sinks

    def base_dir(self) -> Path:
        return self._sinks[0].base_dir()

    def write_mdl(self, *, source_id: str, schema: str, table: str, mdl: GeneratedMDL) -> Path:
        last_path: Path | None = None
        for sink in self._sinks:
            last_path = sink.write_mdl(source_id=source_id, schema=schema, table=table, mdl=mdl)
        return last_path  # type: ignore[return-value]

    def write_annotations(
        self, *, source_id: str, schema: str, table: str, annotations: AssetAnnotations,
    ) -> Path:
        last_path: Path | None = None
        for sink in self._sinks:
            last_path = sink.write_annotations(
                source_id=source_id, schema=schema, table=table, annotations=annotations,
            )
        return last_path  # type: ignore[return-value]

    def write_inferred_relationships(self, **kwargs: Any) -> None:
        for sink in self._sinks:
            try:
                sink.write_inferred_relationships(**kwargs)
            except AttributeError:
                # Sink doesn't support this side-output method; skip silently.
                continue

    def write_data_protection_hints(self, **kwargs: Any) -> None:
        for sink in self._sinks:
            try:
                sink.write_data_protection_hints(**kwargs)
            except AttributeError:
                continue

    def write_causal_candidates(self, **kwargs: Any) -> None:
        for sink in self._sinks:
            try:
                sink.write_causal_candidates(**kwargs)
            except AttributeError:
                continue

    def write_table_aggregates(self, **kwargs: Any) -> None:
        for sink in self._sinks:
            try:
                sink.write_table_aggregates(**kwargs)
            except AttributeError:
                continue

    def write_table_samples(self, **kwargs: Any) -> None:
        for sink in self._sinks:
            try:
                sink.write_table_samples(**kwargs)
            except AttributeError:
                continue

    def write_relation_schema(self, **kwargs: Any) -> None:
        for sink in self._sinks:
            try:
                sink.write_relation_schema(**kwargs)
            except AttributeError:
                continue


def make_sink(cfg: OutputConfig, *, org_id: str | None = None) -> Sink:
    """Factory. Supports kind='filesystem' | 'hierarchy_store' | 'tee' | 'preview'."""
    if cfg.kind == "filesystem":
        return FilesystemSink(cfg)
    if cfg.kind == "hierarchy_store":
        if not org_id:
            raise ValueError("hierarchy_store sink requires org_id")
        return HierarchyStoreSink(org_id=org_id)
    if cfg.kind == "tee":
        # Tee = filesystem + hierarchy_store. The config carries `base_dir` for
        # the filesystem half; the store reads its URL from the env.
        if not org_id:
            raise ValueError("tee sink requires org_id")
        return TeeSink([FilesystemSink(cfg), HierarchyStoreSink(org_id=org_id)])
    if cfg.kind == "preview":
        # Local-only: writes everything FilesystemSink writes PLUS Postgres-
        # bound row dumps + Qdrant-bound event dumps under <base_dir>/postgres
        # and <base_dir>/qdrant. Use with `source.kind=local_files` for a
        # fully offline run.
        from ontology_pipeline.output_preview import PreviewSink
        return PreviewSink(cfg, org_id=org_id or "preview-org")
    raise ValueError(f"Unsupported output kind {cfg.kind!r}")
