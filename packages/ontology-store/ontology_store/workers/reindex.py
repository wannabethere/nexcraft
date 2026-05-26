"""ReindexWorker — drains the reindex queue into Qdrant.

Long-running. Polls `reindex_queue` in batches. Per task:
  - hydrate the affected row(s) from Postgres,
  - build narrative + payload via `workers.narrative`,
  - upsert via `vector.HierarchyVectorIndexer`,
  - mark the task done.

Failures retry up to `max_attempts` (default 5) with exponential backoff at
the queue level: the row is reset to `pending` with an incremented attempts
count; the worker picks it up again on its next dequeue pass.

Concurrency:
  - Single-process worker calls `run_forever()` in a thread or as a separate
    process (`ontology-store reindex` CLI).
  - Multi-process workers compete safely via `FOR UPDATE SKIP LOCKED` in the
    DAO's `dequeue_batch`.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Any, Callable

from sqlalchemy import select
from sqlalchemy.orm import Session

from ontology_store.dao.reader import AssetReader
from ontology_store.db.engine import Database
from ontology_store.db.models import (
    ClusterMetadata,
    SchemaMetadata,
    Source,
)
from ontology_store.workers.narrative import (
    build_asset_narrative,
    build_asset_payload,
    build_card_narrative,
    build_card_payload,
    build_schema_narrative,
    build_schema_payload,
    build_source_narrative,
    build_source_payload,
)
from ontology_store.workers.queue import QueueDAO, QueueTask, TaskKind

logger = logging.getLogger(__name__)


@dataclass
class ReindexWorkerStats:
    processed: int = 0
    succeeded: int = 0
    failed: int = 0
    skipped: int = 0


class ReindexWorker:
    """Polls `reindex_queue` and applies tasks to Qdrant.

    Args:
        database: ontology-store Database.
        indexer:  HierarchyVectorIndexer instance (with embedder + client).
        batch_size:           tasks pulled per dequeue.
        poll_interval_seconds: idle sleep between empty polls.
        max_attempts:         per-task retries before marking failed.
        task_kinds:           optional whitelist of task kinds to process. Default: all qdrant_* kinds.
    """

    DEFAULT_TASK_KINDS: tuple[str, ...] = (
        TaskKind.QDRANT_ASSET.value,
        TaskKind.QDRANT_FIELD.value,
        TaskKind.QDRANT_CARD.value,
        TaskKind.QDRANT_SOURCE.value,
        TaskKind.QDRANT_SCHEMA.value,
        TaskKind.QDRANT_CATALOG.value,
        TaskKind.EVENT_CAUSAL.value,
        TaskKind.EVENT_RELATION.value,
        TaskKind.EVENT_PROTECTION.value,
        TaskKind.EVENT_CARD.value,
    )

    def __init__(
        self,
        *,
        database: Database,
        indexer: Any,  # HierarchyVectorIndexer
        batch_size: int = 10,
        poll_interval_seconds: float = 2.0,
        max_attempts: int = 5,
        task_kinds: list[str] | None = None,
    ) -> None:
        self.db = database
        self.indexer = indexer
        self.batch_size = batch_size
        self.poll_interval_seconds = poll_interval_seconds
        self.max_attempts = max_attempts
        self.task_kinds = list(task_kinds) if task_kinds else list(self.DEFAULT_TASK_KINDS)

        # Per-task-kind handler dispatch
        self._handlers: dict[str, Callable[[Session, QueueTask], None]] = {
            TaskKind.QDRANT_ASSET.value:   self._handle_asset,
            TaskKind.QDRANT_FIELD.value:   self._handle_field_stub,
            TaskKind.QDRANT_CARD.value:    self._handle_card,
            TaskKind.QDRANT_SOURCE.value:  self._handle_source,
            TaskKind.QDRANT_SCHEMA.value:  self._handle_schema,
            TaskKind.QDRANT_CATALOG.value: self._handle_catalog_stub,
            TaskKind.EVENT_CAUSAL.value:      self._handle_event_causal,
            TaskKind.EVENT_RELATION.value:    self._handle_event_relation,
            TaskKind.EVENT_PROTECTION.value:  self._handle_event_protection,
            TaskKind.EVENT_CARD.value:        self._handle_event_card,
        }

    # ── Public entry points ────────────────────────────────────────────

    def run_once(self, *, limit: int | None = None) -> ReindexWorkerStats:
        """Process up to `limit` tasks (or one batch if limit is None) and return."""
        stats = ReindexWorkerStats()
        with self.db.session() as session:
            tasks = QueueDAO(session).dequeue_batch(
                batch_size=limit or self.batch_size, task_kinds=self.task_kinds,
            )
        if not tasks:
            return stats
        for task in tasks:
            self._process_task(task, stats)
        return stats

    def run_forever(self, *, stop_signal: Callable[[], bool] | None = None) -> None:
        """Long-running loop. Optionally takes a `stop_signal` callable that
        returns True when the worker should exit (for graceful shutdown).
        """
        logger.info(
            "ReindexWorker starting (batch=%d, poll=%.1fs, kinds=%s)",
            self.batch_size, self.poll_interval_seconds, self.task_kinds,
        )
        while True:
            if stop_signal is not None and stop_signal():
                logger.info("ReindexWorker stop signal received; exiting")
                return
            stats = self.run_once()
            if stats.processed == 0:
                time.sleep(self.poll_interval_seconds)
            else:
                logger.info(
                    "ReindexWorker batch: processed=%d ok=%d failed=%d skipped=%d",
                    stats.processed, stats.succeeded, stats.failed, stats.skipped,
                )

    # ── Per-task processing ────────────────────────────────────────────

    def _process_task(self, task: QueueTask, stats: ReindexWorkerStats) -> None:
        stats.processed += 1
        handler = self._handlers.get(task.task_kind)
        if handler is None:
            # Unknown kind — mark done (we don't want it to clog the queue forever).
            with self.db.session() as session:
                QueueDAO(session).mark_done(task.queue_id)
            stats.skipped += 1
            logger.warning("Unknown task kind %r; marked done (queue_id=%d)", task.task_kind, task.queue_id)
            return

        try:
            with self.db.session() as session:
                handler(session, task)
                QueueDAO(session).mark_done(task.queue_id)
            stats.succeeded += 1
        except Exception as exc:
            stats.failed += 1
            logger.exception("Task %d (%s) failed: %s", task.queue_id, task.task_kind, exc)
            try:
                with self.db.session() as session:
                    QueueDAO(session).mark_failed_retry(
                        task.queue_id, error=str(exc), max_attempts=self.max_attempts,
                    )
            except Exception as inner:
                logger.error(
                    "Failed to mark task %d as retry-pending: %s", task.queue_id, inner,
                )

    # ── Handlers ───────────────────────────────────────────────────────

    def _handle_asset(self, session: Session, task: QueueTask) -> None:
        asset_rk = task.payload.get("asset_rk")
        if not asset_rk:
            logger.warning("qdrant_asset task %d missing asset_rk; skipping", task.queue_id)
            return
        ctx = AssetReader(session).get_asset(asset_rk)
        if ctx is None:
            logger.info(
                "qdrant_asset: asset %s no longer in spine; treating as no-op", asset_rk,
            )
            return
        text = build_asset_narrative(ctx)
        payload = build_asset_payload(ctx)
        # Patch org_id from source row if not populated by build_asset_payload
        if payload.get("org_id") is None and ctx.source_id:
            source_row = session.get(Source, ctx.source_id)
            if source_row is not None:
                payload["org_id"] = source_row.org_id
        self.indexer.upsert_asset(asset_rk, text=text, payload=payload)

    def _handle_source(self, session: Session, task: QueueTask) -> None:
        source_id = task.payload.get("source_id")
        if not source_id:
            return
        source = session.get(Source, source_id)
        if source is None:
            return
        text = build_source_narrative(
            display_name=source.display_name,
            purpose=source.purpose,
            business_context=source.business_context,
            role=source.role,
            entities=[],  # entity_authority_claim join deferred
        )
        payload = build_source_payload(
            source_id=source.source_id, org_id=source.org_id, kind=source.kind,
            role=source.role, environment=source.environment,
        )
        self.indexer.upsert_source(source_id, text=text, payload=payload)

    def _handle_schema(self, session: Session, task: QueueTask) -> None:
        schema_rk = task.payload.get("schema_rk")
        if not schema_rk:
            return
        schema = session.get(SchemaMetadata, schema_rk)
        if schema is None:
            return
        # schema_ext is optional in v1
        from ontology_store.db.models import SchemaCatalog, SchemaExt
        ext = session.get(SchemaExt, schema_rk)
        sc = session.get(SchemaCatalog, schema_rk)
        # source/org via cluster_rk
        cluster = session.get(ClusterMetadata, schema.cluster_rk)
        from ontology_store.dao.reader import _source_id_from_cluster_rk
        source_id = _source_id_from_cluster_rk(cluster.rk) if cluster else ""
        source_row = session.get(Source, source_id) if source_id else None
        org_id = source_row.org_id if source_row else ""

        text = build_schema_narrative(
            display_name=(ext.display_name if ext else schema.name),
            description=None,  # schema_description join deferred
            purpose=(ext.purpose if ext else None),
            domain_tags=(ext.domain_tags if ext else []),
        )
        payload = build_schema_payload(
            schema_rk=schema_rk,
            schema_name=schema.name,
            org_id=org_id,
            source_id=source_id,
            catalog_uid=(sc.catalog_uid if sc else None),
            lifecycle_stage=(ext.lifecycle_stage if ext else "production"),
            domain_tags=(ext.domain_tags if ext else []),
        )
        self.indexer.upsert_schema(schema_rk, text=text, payload=payload)

    def _handle_card(self, session: Session, task: QueueTask) -> None:
        """Card-storage tables aren't in v1 of the store yet. The handler is wired
        for forward-compat; payload is expected to carry full body+frontmatter for
        callers that supply it directly. Otherwise it's a no-op.
        """
        tenant_id = task.payload.get("tenant_id")
        card_id = task.payload.get("card_id")
        kind = task.payload.get("card_kind")
        body = task.payload.get("body")
        if not (tenant_id and card_id and kind and body):
            logger.info(
                "qdrant_card task %d missing required payload (tenant_id/card_id/kind/body); skipping",
                task.queue_id,
            )
            return
        text = build_card_narrative(body=body, aliases=task.payload.get("aliases"))
        payload = build_card_payload(
            layer=task.payload.get("layer", "semantic"),
            kind=kind,
            card_id=card_id,
            markings=task.payload.get("markings"),
            refs=task.payload.get("refs"),
            origin=task.payload.get("origin", "tenant"),
            deprecated=bool(task.payload.get("deprecated", False)),
        )
        point_id = f"{tenant_id}::semantic::{kind}::{card_id}"
        self.indexer.upsert_card(tenant_id, point_id=point_id, body=text, payload=payload)

    def _handle_field_stub(self, session: Session, task: QueueTask) -> None:
        """T5 field reindex — deferred (column_ext + descriptions wiring needed)."""
        logger.debug("qdrant_field handler is a v1 stub; task %d treated as no-op", task.queue_id)

    def _handle_catalog_stub(self, session: Session, task: QueueTask) -> None:
        """T2 catalog reindex — deferred (full payload + description model)."""
        logger.debug("qdrant_catalog handler is a v1 stub; task %d treated as no-op", task.queue_id)

    # ── Event-sourced handlers ──────────────────────────────────────────
    #
    # Each handler:
    #   1. Looks up the Postgres row by id from the queue payload.
    #   2. Calls the corresponding builder from `event_narrative.py`.
    #   3. Appends to the right *_events collection via the indexer.
    #
    # Payload contract for an EVENT_* task:
    #   { "tenant_id": str,
    #     "row_id":    int (PK of the source table),
    #     "run_id":    str | None,
    #     "org_id":    str | None,
    #     "source_id": str | None,
    #     "is_new":    bool (CARD only — author vs. revised) }

    def _handle_event_causal(self, session: Session, task: QueueTask) -> None:
        from ontology_store.db.inference_models import CausalCandidate
        from ontology_store.workers.event_narrative import build_causal_candidate_event

        tenant_id = task.payload.get("tenant_id")
        row_id = task.payload.get("row_id")
        if not (tenant_id and row_id):
            logger.warning("event_causal task %d missing tenant_id/row_id", task.queue_id)
            return
        row = session.get(CausalCandidate, int(row_id))
        if row is None:
            logger.info("event_causal: candidate_id=%s no longer exists; skipping", row_id)
            return
        envelope, narrative, extra = build_causal_candidate_event(
            row=row,
            run_id=task.payload.get("run_id"),
            org_id=task.payload.get("org_id"),
            source_id=task.payload.get("source_id"),
        )
        self.indexer.append_causal_event(
            tenant_id, envelope=envelope, narrative=narrative, extra_payload=extra,
        )

    def _handle_event_relation(self, session: Session, task: QueueTask) -> None:
        from ontology_store.db.relation_models import RelationType
        from ontology_store.workers.event_narrative import build_relation_type_event

        tenant_id = task.payload.get("tenant_id")
        row_id = task.payload.get("row_id")
        if not (tenant_id and row_id):
            logger.warning("event_relation task %d missing tenant_id/row_id", task.queue_id)
            return
        row = session.get(RelationType, int(row_id))
        if row is None:
            logger.info(
                "event_relation: relation_type_pk=%s no longer exists; skipping", row_id,
            )
            return
        envelope, narrative, extra = build_relation_type_event(
            row=row, run_id=task.payload.get("run_id"),
        )
        self.indexer.append_relation_event(
            tenant_id, envelope=envelope, narrative=narrative, extra_payload=extra,
        )

    def _handle_event_protection(self, session: Session, task: QueueTask) -> None:
        from ontology_store.db.inference_models import DataProtectionHint
        from ontology_store.workers.event_narrative import build_data_protection_event

        tenant_id = task.payload.get("tenant_id")
        row_id = task.payload.get("row_id")
        if not (tenant_id and row_id):
            logger.warning("event_protection task %d missing tenant_id/row_id", task.queue_id)
            return
        row = session.get(DataProtectionHint, int(row_id))
        if row is None:
            logger.info(
                "event_protection: hint_id=%s no longer exists; skipping", row_id,
            )
            return
        envelope, narrative, extra = build_data_protection_event(
            row=row,
            run_id=task.payload.get("run_id"),
            org_id=task.payload.get("org_id"),
        )
        self.indexer.append_protection_event(
            tenant_id, envelope=envelope, narrative=narrative, extra_payload=extra,
        )

    def _handle_event_card(self, session: Session, task: QueueTask) -> None:
        from ontology_store.db.card_models import Card
        from ontology_store.workers.event_narrative import build_card_event

        tenant_id = task.payload.get("tenant_id")
        row_id = task.payload.get("row_id")
        if not (tenant_id and row_id):
            logger.warning("event_card task %d missing tenant_id/row_id", task.queue_id)
            return
        row = session.get(Card, int(row_id))
        if row is None:
            logger.info("event_card: card_pk=%s no longer exists; skipping", row_id)
            return
        envelope, narrative, extra = build_card_event(
            row=row,
            is_new=bool(task.payload.get("is_new", False)),
            run_id=task.payload.get("run_id"),
        )
        self.indexer.append_card_event(
            tenant_id, envelope=envelope, narrative=narrative, extra_payload=extra,
        )
