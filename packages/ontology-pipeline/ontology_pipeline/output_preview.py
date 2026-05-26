"""PreviewSink — local-only sink that captures everything the real backends
would receive.

When you run the pipeline with `output.kind=preview`, every artifact that
WOULD go into Postgres / Qdrant is written to disk under `base_dir`:

  output/preview/
  ├── mdl/<source>/<schema>/<table>.json            (FilesystemSink core)
  ├── annotations/<source>/<schema>/<table>.json
  ├── column_stats/<source>/<schema>/<table>.aggregates.json
  ├── column_stats/<source>/<schema>/<table>.samples.json
  ├── inferred_relationships/<source>/<schema>/<table>.json
  ├── data_protection_hints/<source>/<schema>/<table>.json
  ├── causal_candidates/<source>/<schema>/<table>.json
  ├── relation_schema/<source>/relation_schema.json
  │
  ├── postgres/                                      # rows that WOULD be persisted
  │   ├── causal_candidate/<rk-safe>.json
  │   ├── data_protection_hint/<rk-safe>.json
  │   ├── relation_type/<pred>__<dom>__<range>.json
  │   ├── column_stat/<column-rk-safe>.json
  │   ├── table_stat/<table-rk-safe>.json
  │   └── lineage_edge/<from>__<to>__<edge_kind>.json
  │
  ├── qdrant/                                        # events that WOULD be indexed
  │   ├── causal_events/<event_id>.json
  │   ├── relation_events/<event_id>.json
  │   ├── protection_events/<event_id>.json
  │   ├── card_events/<event_id>.json
  │   └── _spine/
  │       ├── hier_t4_assets/<rk-safe>.json          (for upsert_asset)
  │       └── hier_t5_fields/<rk-safe>.json          (for upsert_field, future)
  │
  └── reindex_queue.jsonl                            # one line per task that WOULD enqueue

The sink extends FilesystemSink — the core MDL / annotations / side-output
writes are unchanged — and adds Postgres/Qdrant artifact dumps for the
shapes that would otherwise live behind a real backend.

Event payloads are built using the EXACT same `event_narrative` builders
the real ReindexWorker uses, so the dump is faithful to production
behavior — not a mock.
"""
from __future__ import annotations

import json
import logging
import re
from dataclasses import asdict, is_dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ontology_pipeline.config import OutputConfig
from ontology_pipeline.output import FilesystemSink, _atomic_write_json

logger = logging.getLogger(__name__)


# ───────────────────────────────────────────────────────────────────────────
# PreviewSink
# ───────────────────────────────────────────────────────────────────────────


class PreviewSink(FilesystemSink):
    """Extends FilesystemSink with Postgres-bound + Qdrant-bound artifact dumps.

    Drop-in for HierarchyStoreSink in local runs. The two extra trees
    (`postgres/`, `qdrant/`) are what an operator inspects to validate
    "would this run produce sane DB rows + sane vector points?"
    """

    def __init__(self, cfg: OutputConfig, *, org_id: str = "preview-org") -> None:
        super().__init__(cfg)
        self._org_id = org_id
        self._queue_path = self._base / "reindex_queue.jsonl"

    # ── Postgres-bound row dumps ────────────────────────────────────────

    def write_causal_candidates(
        self,
        *,
        source_id: str,
        schema: str,
        table: str,
        candidates: list[dict[str, Any]],
    ) -> None:
        # Keep the per-table filesystem dump (parent class).
        super().write_causal_candidates(
            source_id=source_id, schema=schema, table=table, candidates=candidates,
        )
        # Plus: one Postgres row + one Qdrant event per candidate.
        for c in candidates:
            row = self._candidate_to_pg_row(c)
            self._write_pg_row("causal_candidate", row,
                               name_parts=[c["asset_rk"], c["subject_ref"],
                                           c["predicate"], c["object_ref"]])
            self._emit_event_for_causal_candidate(row, source_id=source_id)

    def write_data_protection_hints(
        self,
        *,
        source_id: str,
        schema: str,
        table: str,
        asset_rk: str,
        hints: dict[str, Any],
    ) -> None:
        super().write_data_protection_hints(
            source_id=source_id, schema=schema, table=table,
            asset_rk=asset_rk, hints=hints,
        )
        if not hints:
            return
        row = self._protection_hint_to_pg_row(asset_rk=asset_rk, hints=hints)
        # Fold any surface fields the enricher attached onto `hints` (asset
        # name / description / surface / column_lookup) into the row so the
        # event builder sees them. The column_lookup in particular lets the
        # protection narrative render each CLS column with its native
        # description and any PII flags.
        for k in (
            "asset_name", "asset_description", "asset_one_liner",
            "asset_surface", "column_lookup",
        ):
            if k in hints and hints[k]:
                row[k] = hints[k]
        self._write_pg_row("data_protection_hint", row, name_parts=[asset_rk])
        self._emit_event_for_protection_hint(row)

    def write_relation_schema(
        self,
        *,
        source_id: str,
        types: list[Any],
        attachments: list[dict[str, Any]],
    ) -> None:
        super().write_relation_schema(
            source_id=source_id, types=types, attachments=attachments,
        )
        for spec in types:
            row = self._relation_type_to_pg_row(spec)
            self._write_pg_row(
                "relation_type", row,
                name_parts=[row["predicate"], row["domain"], row["range_type"]],
            )
            self._emit_event_for_relation_type(row)
        for a in attachments:
            self._write_pg_row(
                "lineage_edge",
                {
                    "from_rk": a["from_rk"], "to_rk": a["to_rk"],
                    "edge_kind": a["edge_kind"],
                    "evidence_kind": "inferred_relationship",
                    "predicate": a["predicate"],
                    "predicate_domain": a["domain"],
                    "predicate_range": a["range_type"],
                },
                name_parts=[a["from_rk"], a["to_rk"], a["edge_kind"]],
            )
            self._emit_event_for_predicate_attached(a)

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
    ) -> None:
        super().write_table_aggregates(
            source_id=source_id, schema=schema, table=table,
            table_rk=table_rk, aggregates=aggregates,
            population_row_count=population_row_count,
            source_system=source_system,
        )
        # Per-column rows for the Postgres-bound view
        for agg in aggregates:
            row = _dc_to_dict(agg)
            self._write_pg_row(
                "column_stat", row, name_parts=[row.get("column_rk", "")],
            )
        # Table-level row
        self._write_pg_row(
            "table_stat",
            {
                "table_rk": table_rk,
                "population_row_count": population_row_count,
                "source_system": source_system,
                "sample_row_count": None,
                "samples_persisted": False,
            },
            name_parts=[table_rk],
        )

    def write_table_samples(
        self,
        *,
        source_id: str,
        schema: str,
        table: str,
        table_facts: Any,
        column_top_frequencies: dict[str, list[dict[str, Any]]],
        pii_safe_column_rks: set[str],
    ) -> None:
        super().write_table_samples(
            source_id=source_id, schema=schema, table=table,
            table_facts=table_facts,
            column_top_frequencies=column_top_frequencies,
            pii_safe_column_rks=pii_safe_column_rks,
        )
        # Update the table_stat row with sample info
        self._write_pg_row(
            "table_stat",
            {
                "table_rk": getattr(table_facts, "table_rk", None),
                "population_row_count": getattr(table_facts, "population_row_count", None),
                "sample_row_count": getattr(table_facts, "sample_row_count", None),
                "source_system": getattr(table_facts, "source_system", None),
                "samples_persisted": True,
                "sample_rows_preview_first": (
                    (getattr(table_facts, "sample_rows", []) or [None])[0]
                ),
            },
            name_parts=[getattr(table_facts, "table_rk", "")],
        )
        # Top-frequencies per (gated) column
        for col_rk, freqs in column_top_frequencies.items():
            if col_rk not in pii_safe_column_rks:
                continue
            self._write_pg_row(
                "column_stat",
                {
                    "column_rk": col_rk, "top_frequencies": freqs,
                    "samples_persisted": True,
                },
                name_parts=[col_rk + "/samples"],
            )

    # ── Internals: pg-row builders ──────────────────────────────────────

    @staticmethod
    def _candidate_to_pg_row(c: dict[str, Any]) -> dict[str, Any]:
        """Mirror what `InferenceDAO.upsert_causal_candidate` would write.

        The PG row carries the canonical (rk-based) fields. Human-readable
        surface fields ride alongside so the SAME dict feeds the Qdrant
        event builder without re-querying for asset metadata. Per-side
        column lookups let the event builder render each evidence column
        with its full brief.
        """
        return {
            # Canonical (rk-keyed) fields — the actual Postgres columns
            "asset_rk": c["asset_rk"],
            "subject_ref": c["subject_ref"],
            "predicate": c["predicate"],
            "object_ref": c["object_ref"],
            "evidence_columns": c.get("evidence_columns") or [],
            "evidence_object_columns": c.get("evidence_object_columns") or [],
            "mechanism_hint": c.get("mechanism_hint") or "",
            "confidence": c.get("confidence"),
            "rationale": c.get("rationale") or c.get("mechanism_hint"),
            "status": c.get("status", "proposed"),
            "provenance": c.get("provenance", "llm_causal_dependency"),
            # Surface fields — propagated to Qdrant payload + narrative
            "asset_name": c.get("asset_name") or "",
            "asset_description": c.get("asset_description"),
            "subject_one_liner": c.get("subject_one_liner") or "",
            "object_one_liner": c.get("object_one_liner") or "",
            "subject_asset_surface": c.get("subject_asset_surface") or "",
            "object_asset_surface": c.get("object_asset_surface") or "",
            "subject_column_brief": c.get("subject_column_brief") or "",
            "object_column_brief": c.get("object_column_brief") or "",
            # Per-side column lookups — every evidence column resolved to its
            # full brief at event-narration time.
            "subject_column_lookup": c.get("subject_column_lookup") or {},
            "object_column_lookup": c.get("object_column_lookup") or {},
        }

    @staticmethod
    def _protection_hint_to_pg_row(
        *, asset_rk: str, hints: dict[str, Any],
    ) -> dict[str, Any]:
        return {
            "asset_rk": asset_rk,
            "rls_predicates": hints.get("rls_predicates") or [],
            "cls_columns": hints.get("cls_columns") or [],
            "rationale": hints.get("rationale") or "",
            "provenance": hints.get("provenance") or "llm_data_protection",
            "status": "proposed",
        }

    @staticmethod
    def _relation_type_to_pg_row(spec: Any) -> dict[str, Any]:
        d = _dc_to_dict(spec)
        return {
            "predicate": d.get("predicate"),
            "domain": d.get("domain"),
            "range_type": d.get("range_type"),
            "inverse": d.get("inverse"),
            "functional": bool(d.get("functional", False)),
            "confidence": d.get("confidence"),
            "evidence_count": int(d.get("evidence_count", 0) or 0),
            "surfaces": d.get("surfaces") or [],
            "provenance": d.get("provenance") or "induce_schema",
        }

    # ── Internals: Qdrant event emission ────────────────────────────────

    def _emit_event_for_causal_candidate(
        self, row: dict[str, Any], *, source_id: str,
    ) -> None:
        """Call the same builder the real reindex worker would call."""
        try:
            from ontology_store.workers.event_narrative import (
                build_causal_candidate_event,
            )
        except ImportError:
            return  # ontology-store not installed — skip
        from types import SimpleNamespace
        envelope, narrative, extra = build_causal_candidate_event(
            row=SimpleNamespace(**row),
            org_id=self._org_id,
            source_id=source_id,
        )
        self._write_event(
            collection="causal_events",
            envelope=envelope, narrative=narrative, extra=extra,
        )

    def _emit_event_for_protection_hint(
        self, row: dict[str, Any],
    ) -> None:
        try:
            from ontology_store.workers.event_narrative import (
                build_data_protection_event,
            )
        except ImportError:
            return
        from types import SimpleNamespace
        envelope, narrative, extra = build_data_protection_event(
            row=SimpleNamespace(**row),
            org_id=self._org_id,
        )
        self._write_event(
            collection="protection_events",
            envelope=envelope, narrative=narrative, extra=extra,
        )

    def _emit_event_for_relation_type(
        self, row: dict[str, Any],
    ) -> None:
        try:
            from ontology_store.workers.event_narrative import (
                build_relation_type_event,
            )
        except ImportError:
            return
        from types import SimpleNamespace
        # surfaces may be a list; the builder accepts both
        envelope, narrative, extra = build_relation_type_event(
            row=SimpleNamespace(org_id=self._org_id, **row),
        )
        self._write_event(
            collection="relation_events",
            envelope=envelope, narrative=narrative, extra=extra,
        )

    def _emit_event_for_predicate_attached(self, a: dict[str, Any]) -> None:
        try:
            from ontology_store.workers.event_narrative import (
                build_predicate_attached_event,
            )
        except ImportError:
            return
        envelope, narrative, extra = build_predicate_attached_event(
            from_rk=a["from_rk"], to_rk=a["to_rk"], edge_kind=a["edge_kind"],
            predicate=a["predicate"], domain=a["domain"], range_type=a["range_type"],
            org_id=self._org_id,
            from_one_liner=a.get("from_one_liner"),
            to_one_liner=a.get("to_one_liner"),
            from_surface=a.get("from_asset_surface"),
            to_surface=a.get("to_asset_surface"),
            from_column=a.get("from_column"),
            to_column=a.get("to_column"),
            from_column_brief=a.get("from_column_brief"),
            to_column_brief=a.get("to_column_brief"),
        )
        self._write_event(
            collection="relation_events",
            envelope=envelope, narrative=narrative, extra=extra,
        )

    def _write_event(
        self, *, collection: str, envelope: Any, narrative: str,
        extra: dict[str, Any],
    ) -> None:
        """One JSON file per event under `qdrant/<collection>/<event_id>.json`.

        Also appends a single-line JSON record to `reindex_queue.jsonl` —
        what the real ReindexWorker would dequeue.
        """
        target = (
            self._base / "qdrant" / collection / f"{envelope.event_id}.json"
        )
        payload = {**envelope.to_qdrant_payload(), **extra}
        _atomic_write_json(target, {
            "event_id": envelope.event_id,
            "collection": collection,
            "narrative": narrative,
            "payload": payload,
        })
        self._append_queue_record({
            "task_kind": _COLLECTION_TO_TASK_KIND.get(collection, "event_unknown"),
            "tenant_id": self._org_id,
            "event_id": envelope.event_id,
            "subject_rk": envelope.subject_rk,
            "produced_at": envelope.produced_at.isoformat(),
            "preview_target": str(target.relative_to(self._base)),
        })

    # ── Helpers ─────────────────────────────────────────────────────────

    def _write_pg_row(
        self,
        table: str,
        row: dict[str, Any],
        *,
        name_parts: list[str],
    ) -> None:
        safe = _safe_filename("__".join(p for p in name_parts if p))
        target = self._base / "postgres" / table / f"{safe}.json"
        _atomic_write_json(target, row)

    def _append_queue_record(self, record: dict[str, Any]) -> None:
        self._queue_path.parent.mkdir(parents=True, exist_ok=True)
        record_with_ts = {
            "queued_at": datetime.now(timezone.utc).isoformat(),
            **record,
        }
        with self._queue_path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record_with_ts, default=str) + "\n")


# ───────────────────────────────────────────────────────────────────────────
# Helpers (module-level)
# ───────────────────────────────────────────────────────────────────────────


_COLLECTION_TO_TASK_KIND = {
    "causal_events":     "event_causal",
    "relation_events":   "event_relation",
    "protection_events": "event_protection",
    "card_events":       "event_card",
}

# Characters not safe in filenames on macOS/Linux/Windows.
_FILENAME_BAD = re.compile(r"[^A-Za-z0-9._-]+")


def _safe_filename(s: str) -> str:
    """Turn an asset_rk or compound key into a filesystem-safe filename.

    `postgres://csod-pg/db/public/employee.due_date` →
        `postgres__csod-pg_db_public_employee.due_date`
    """
    s = s.replace("://", "__")
    s = _FILENAME_BAD.sub("_", s)
    # Cap length — filesystems vary, but 200 is well under every common limit.
    if len(s) > 200:
        s = s[:190] + "_truncated"
    return s


def _dc_to_dict(obj: Any) -> dict[str, Any]:
    """Best-effort dataclass / Pydantic / plain-object → dict."""
    if is_dataclass(obj):
        return asdict(obj)
    if hasattr(obj, "model_dump"):
        return obj.model_dump(mode="json")
    if isinstance(obj, dict):
        return dict(obj)
    return dict(vars(obj))
