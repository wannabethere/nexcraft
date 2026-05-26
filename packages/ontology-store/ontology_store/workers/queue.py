"""Reindex queue — model + DAO.

Per `hierarchy_persistence_and_ingestion_spec.md` §4.1. Tasks are enqueued by
the data-writing DAOs (Hierarchy, Annotation) after successful Postgres commits.
A worker pulls pending tasks in batches, processes them, and marks them done
or failed-with-retry.

Concurrency model — v1:
  - Single worker per task_kind is fine.
  - Multi-worker is supported via `SELECT ... FOR UPDATE SKIP LOCKED` in
    `dequeue_batch()`; each worker pulls a disjoint batch.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Any

from sqlalchemy import (
    DateTime,
    Integer,
    Text,
    func,
    select,
    text as sql_text,
    update,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, Session, mapped_column

from ontology_store.db.engine import Base
from ontology_store.db.models import HierarchyAudit

logger = logging.getLogger(__name__)


# ───────────────────────────────────────────────────────────────────────────
# Enums + dataclass for in-flight task representation
# ───────────────────────────────────────────────────────────────────────────

class TaskKind(StrEnum):
    # Doc-per-row reindex tasks — spine + per-tenant authoring collections.
    QDRANT_ASSET = "qdrant_asset"
    QDRANT_FIELD = "qdrant_field"
    QDRANT_CARD = "qdrant_card"
    QDRANT_SOURCE = "qdrant_source"
    QDRANT_SCHEMA = "qdrant_schema"
    QDRANT_CATALOG = "qdrant_catalog"
    # Event-sourced append tasks — one per inference / extraction event.
    # Payload carries the event envelope + narrative text directly (or DB
    # ids the handler hydrates).
    EVENT_CAUSAL = "event_causal"
    EVENT_RELATION = "event_relation"
    EVENT_PROTECTION = "event_protection"
    EVENT_CARD = "event_card"
    # Non-Qdrant background tasks (unchanged).
    BUNDLE_ASSET = "bundle_asset"
    BUNDLE_CATALOG = "bundle_catalog"
    LINEAGE_DERIVE = "lineage_derive"
    ANNOTATION_ENRICH = "annotation_enrich"


class TaskStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"


@dataclass
class QueueTask:
    """In-memory task representation pulled from the queue."""
    queue_id: int
    task_kind: str
    payload: dict[str, Any]
    attempts: int
    enqueued_at: datetime


# ───────────────────────────────────────────────────────────────────────────
# ORM model
# ───────────────────────────────────────────────────────────────────────────

class ReindexQueueRow(Base):
    __tablename__ = "reindex_queue"

    queue_id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    task_kind: Mapped[str] = mapped_column(Text, nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    enqueued_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    last_error: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(Text, nullable=False, default=TaskStatus.PENDING.value)


# ───────────────────────────────────────────────────────────────────────────
# DAO
# ───────────────────────────────────────────────────────────────────────────

class QueueDAO:
    """Enqueue + dequeue operations for the reindex queue."""

    def __init__(self, session: Session) -> None:
        self.s = session

    # ── enqueue ────────────────────────────────────────────────────────

    def enqueue(self, *, task_kind: str | TaskKind, payload: dict[str, Any]) -> int:
        """Add a task to the queue. Returns the queue_id."""
        kind_str = task_kind.value if isinstance(task_kind, TaskKind) else str(task_kind)
        row = ReindexQueueRow(
            task_kind=kind_str,
            payload=payload,
            status=TaskStatus.PENDING.value,
            attempts=0,
        )
        self.s.add(row)
        self.s.flush()
        return row.queue_id

    def enqueue_many(self, tasks: list[tuple[str | TaskKind, dict[str, Any]]]) -> int:
        """Bulk-enqueue. Returns count enqueued."""
        rows: list[ReindexQueueRow] = []
        for kind, payload in tasks:
            kind_str = kind.value if isinstance(kind, TaskKind) else str(kind)
            rows.append(ReindexQueueRow(
                task_kind=kind_str, payload=payload,
                status=TaskStatus.PENDING.value, attempts=0,
            ))
        if rows:
            self.s.add_all(rows)
            self.s.flush()
        return len(rows)

    # ── dequeue ────────────────────────────────────────────────────────

    def dequeue_batch(
        self,
        *,
        batch_size: int = 10,
        task_kinds: list[str] | None = None,
    ) -> list[QueueTask]:
        """Atomically claim the next batch of pending tasks.

        Uses `FOR UPDATE SKIP LOCKED` so multiple workers can run concurrently
        without seeing the same rows. Tasks are marked `running` + `started_at`
        is set; `attempts` is incremented.
        """
        # Postgres-specific FOR UPDATE SKIP LOCKED dance, wrapped in a CTE.
        kinds_clause = ""
        params: dict[str, Any] = {"batch_size": batch_size}
        if task_kinds:
            kinds_clause = "AND task_kind = ANY(:task_kinds)"
            params["task_kinds"] = task_kinds

        sql = sql_text(f"""
            WITH next_batch AS (
                SELECT queue_id
                FROM reindex_queue
                WHERE status = 'pending'
                  {kinds_clause}
                ORDER BY enqueued_at
                LIMIT :batch_size
                FOR UPDATE SKIP LOCKED
            )
            UPDATE reindex_queue rq
            SET status = 'running',
                started_at = now(),
                attempts = rq.attempts + 1
            FROM next_batch nb
            WHERE rq.queue_id = nb.queue_id
            RETURNING rq.queue_id, rq.task_kind, rq.payload, rq.attempts, rq.enqueued_at
        """)

        rows = self.s.execute(sql, params).all()
        return [
            QueueTask(
                queue_id=r[0],
                task_kind=r[1],
                payload=r[2] or {},
                attempts=r[3],
                enqueued_at=r[4],
            )
            for r in rows
        ]

    # ── mark done / failed / retry ─────────────────────────────────────

    def mark_done(self, queue_id: int) -> None:
        self.s.execute(
            update(ReindexQueueRow)
            .where(ReindexQueueRow.queue_id == queue_id)
            .values(status=TaskStatus.DONE.value, completed_at=func.now(), last_error=None)
        )

    def mark_failed_retry(self, queue_id: int, error: str, *, max_attempts: int = 5) -> None:
        """If attempts < max, mark pending again for retry. Otherwise mark failed permanently."""
        row = self.s.get(ReindexQueueRow, queue_id)
        if row is None:
            return
        if row.attempts >= max_attempts:
            row.status = TaskStatus.FAILED.value
            row.completed_at = datetime.utcnow()
        else:
            row.status = TaskStatus.PENDING.value
            row.started_at = None
        row.last_error = error[:2000] if error else None

    # ── stats ──────────────────────────────────────────────────────────

    def depth(self, *, status: str | None = None, task_kind: str | None = None) -> int:
        stmt = select(func.count()).select_from(ReindexQueueRow)
        if status:
            stmt = stmt.where(ReindexQueueRow.status == status)
        if task_kind:
            stmt = stmt.where(ReindexQueueRow.task_kind == task_kind)
        return int(self.s.execute(stmt).scalar_one())


# ───────────────────────────────────────────────────────────────────────────
# Convenience module-level enqueue helpers — used by DAOs and call sites
# that have a session in hand.
# ───────────────────────────────────────────────────────────────────────────

def enqueue_asset_reindex(session: Session, *, asset_rk: str, asset_kind: str = "table") -> int:
    """Enqueue a Qdrant reindex task for an asset rk."""
    return QueueDAO(session).enqueue(
        task_kind=TaskKind.QDRANT_ASSET,
        payload={"asset_rk": asset_rk, "asset_kind": asset_kind},
    )


def enqueue_card_reindex(session: Session, *, tenant_id: str, card_id: str, kind: str) -> int:
    """Enqueue a Qdrant reindex task for a card."""
    return QueueDAO(session).enqueue(
        task_kind=TaskKind.QDRANT_CARD,
        payload={"tenant_id": tenant_id, "card_id": card_id, "card_kind": kind},
    )
