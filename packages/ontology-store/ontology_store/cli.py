"""ontology-store CLI — runs operational workers (reindex, in v1).

Usage:
    ontology-store reindex run-forever [--env prod] [--batch 10] [--poll 2.0]
    ontology-store reindex run-once   [--limit 50]
    ontology-store reindex status

The Database URL is read from $ONTOLOGY_STORE_URL.
Qdrant + OpenAI need $QDRANT_URL (or QDRANT_HOST) + $OPENAI_API_KEY.
"""
from __future__ import annotations

import logging
import os
import sys

try:
    import click
except ImportError as exc:
    raise ImportError(
        "ontology-store CLI requires click. Install with: pip install 'ontology-store[dev]' or just `pip install click`."
    ) from exc


@click.group()
@click.option("--log-level", default="INFO", show_default=True,
              type=click.Choice(["DEBUG", "INFO", "WARNING", "ERROR"], case_sensitive=False))
def main(log_level: str) -> None:
    logging.basicConfig(
        level=log_level.upper(),
        format="%(asctime)s  %(levelname)-7s  %(name)s  %(message)s",
    )


@main.group()
def reindex() -> None:
    """Operate the reindex worker."""


@reindex.command("run-forever")
@click.option("--env", default="prod", show_default=True, help="Env slug for hier_t* collection names.")
@click.option("--batch", default=10, show_default=True, type=int)
@click.option("--poll", default=2.0, show_default=True, type=float, help="Idle sleep seconds.")
@click.option("--max-attempts", default=5, show_default=True, type=int)
def cmd_run_forever(env: str, batch: int, poll: float, max_attempts: int) -> None:
    """Long-running worker loop."""
    db, indexer = _build_worker_deps(env=env)
    from ontology_store.workers import ReindexWorker
    worker = ReindexWorker(
        database=db, indexer=indexer,
        batch_size=batch, poll_interval_seconds=poll, max_attempts=max_attempts,
    )
    try:
        worker.run_forever()
    except KeyboardInterrupt:
        click.echo("Stopped (KeyboardInterrupt).")
        sys.exit(0)


@reindex.command("run-once")
@click.option("--env", default="prod", show_default=True)
@click.option("--limit", default=10, show_default=True, type=int)
def cmd_run_once(env: str, limit: int) -> None:
    """Process up to `limit` tasks and return. Useful for cron / tests."""
    db, indexer = _build_worker_deps(env=env)
    from ontology_store.workers import ReindexWorker
    worker = ReindexWorker(database=db, indexer=indexer, batch_size=limit)
    stats = worker.run_once(limit=limit)
    click.echo(
        f"processed={stats.processed} succeeded={stats.succeeded} "
        f"failed={stats.failed} skipped={stats.skipped}"
    )


@reindex.command("status")
def cmd_status() -> None:
    """Show queue depth by task kind + status."""
    from ontology_store import Database
    from ontology_store.workers.queue import QueueDAO, TaskStatus

    db = Database.from_env()
    with db.session() as s:
        dao = QueueDAO(s)
        statuses = ["pending", "running", "done", "failed"]
        for status in statuses:
            n = dao.depth(status=status)
            click.echo(f"  {status:8s} : {n}")


def _build_worker_deps(*, env: str):
    """Construct (Database, HierarchyVectorIndexer) from env."""
    from ontology_store import Database
    from ontology_store.vector import (
        HierarchyVectorIndexer,
        OpenAIEmbedder,
        QdrantClientFactory,
    )
    if not os.environ.get("ONTOLOGY_STORE_URL"):
        raise click.UsageError("ONTOLOGY_STORE_URL env var must be set")
    if not (os.environ.get("QDRANT_URL") or os.environ.get("QDRANT_HOST")):
        raise click.UsageError("QDRANT_URL or QDRANT_HOST env var must be set")
    if not (
        os.environ.get("OPENAI_API_KEY")
        or os.environ.get("EMBEDDING_API_KEY")
    ):
        raise click.UsageError(
            "OPENAI_API_KEY or EMBEDDING_API_KEY must be set for vector embeddings "
            "(DeepSeek has no embedding API)"
        )

    db = Database.from_env()
    client = QdrantClientFactory.get()
    embedder = OpenAIEmbedder()
    indexer = HierarchyVectorIndexer(qdrant_client=client, embedder=embedder, env=env)
    return db, indexer


if __name__ == "__main__":
    main()
