"""Workers — long-running processes that maintain derived state.

v1 ships the **reindex worker**: drains `reindex_queue` rows that the DAOs
enqueue post-commit, reads the affected rows from Postgres, builds narrative
text + payload per tier, and upserts into the corresponding Qdrant collection
via `HierarchyVectorIndexer`.

Future workers (per hierarchy_persistence_and_ingestion_spec.md §14):
  - bundle-emit worker  — materializes `tenants/<org>/assets/...` JSON bundles
  - reconciler          — nightly drift detection between source state + storage
  - audit-pruner        — archives old `hierarchy_audit` rows to cold storage

All workers share the same pattern: idempotent, resumable, batchable, and
expose a `run_forever()` loop plus a `run_once(limit)` for tests / cron.
"""
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
from ontology_store.workers.queue import (
    QueueDAO,
    QueueTask,
    ReindexQueueRow,
    TaskKind,
    TaskStatus,
    enqueue_asset_reindex,
    enqueue_card_reindex,
)
from ontology_store.workers.reindex import ReindexWorker

__all__ = [
    "QueueDAO",
    "QueueTask",
    "ReindexQueueRow",
    "TaskKind",
    "TaskStatus",
    "enqueue_asset_reindex",
    "enqueue_card_reindex",
    "ReindexWorker",
    "build_asset_narrative",
    "build_asset_payload",
    "build_schema_narrative",
    "build_schema_payload",
    "build_source_narrative",
    "build_source_payload",
    "build_card_narrative",
    "build_card_payload",
]
