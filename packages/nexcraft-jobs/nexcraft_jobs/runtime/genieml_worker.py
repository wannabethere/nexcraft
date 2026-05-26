"""GenieML FedSQL Temporal worker — single Postgres source (iteration 1).

Runs the workflows the GenieML default SQL agent submits:
  - ``nexcraft_fedsql_query``       (single SQL → rows)
  - ``nexcraft_dstools_pipeline``   (SQL extract → dstools python analysis)
plus the recipe / multihop / dstools / output activities, all registered by
``TemporalRuntime`` on task queue ``nexcraft-jobs``.

The FedSQLClient is built from POSTGRES_* env via ``build_postgres_fedsql_client``.

Run (from the nexcraft workspace, Temporal listening on localhost:7233)::

    set -a; source <context_preparer/.env>; set +a
    python -m nexcraft_jobs.runtime.genieml_worker

Env:
    TEMPORAL_TARGET (default localhost:7233), TEMPORAL_NAMESPACE (default 'default'),
    NEXCRAFT_TASK_QUEUE (default 'nexcraft-jobs'), POSTGRES_* (the source),
    NEXCRAFT_DEFAULT_SOURCE_ID (default 'preview'), NEXCRAFT_DOTENV_PATH (optional).
"""

from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path

logger = logging.getLogger("nexcraft_jobs.genieml_worker")


def _parse_dotenv_into_env(p: Path) -> None:
    """Set os.environ from a .env file (setdefault — explicit env wins)."""
    import re

    for raw in p.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        v = value.strip()
        if v[:1] in ("'", '"'):
            # Quoted value — strip matching quotes, keep contents verbatim.
            q = v[0]
            if len(v) >= 2 and v.endswith(q):
                v = v[1:-1]
        else:
            # Strip an inline comment (whitespace + '#...') so `.env` lines like
            # `FEATURE_X=true   # note` don't load the comment into the value.
            v = re.sub(r"\s+#.*$", "", v).strip()
        os.environ.setdefault(key.strip(), v)


def _load_dotenv() -> None:
    """Load worker config from a .env file. Explicit env always wins.

    Order: NEXCRAFT_DOTENV_PATH if set, else auto-discover the first existing of
    ``nexcraft-jobs/.env`` then ``nexcraft/packages/.env`` (GenieML worker
    defaults to the jobs package env).
    """
    explicit = os.environ.get("NEXCRAFT_DOTENV_PATH")
    if explicit:
        p = Path(explicit).expanduser()
        if p.is_file():
            logger.info("Loading worker env from NEXCRAFT_DOTENV_PATH=%s", p)
            _parse_dotenv_into_env(p)
        else:
            logger.warning("NEXCRAFT_DOTENV_PATH=%s not found", explicit)
        return
    here = Path(__file__).resolve()
    for cand in (here.parents[2] / ".env", here.parents[3] / ".env"):
        if cand.is_file():
            logger.info("Loading worker env from %s", cand)
            _parse_dotenv_into_env(cand)
            return
    logger.warning(
        "No .env found (set NEXCRAFT_DOTENV_PATH or place one at nexcraft-jobs/.env)"
    )


async def main() -> None:
    logging.basicConfig(level=logging.INFO)
    _load_dotenv()

    from temporalio.client import Client
    from temporalio.worker import UnsandboxedWorkflowRunner, Worker

    from nexcraft_jobs.runtime.postgres_fedsql import (
        build_postgres_fedsql_client,
        default_source_id,
    )
    from nexcraft_jobs.runtime.temporal_worker_bundle import (
        NEXCRAFT_RECIPE_ACTIVITIES,
        NEXCRAFT_RECIPE_WORKFLOWS,
    )
    from nexcraft_jobs.runtime.worker_config import configure_worker
    from nexcraft_jobs.store.null_store import NullResultStore

    target = os.environ.get("TEMPORAL_TARGET", "localhost:7233").strip()
    namespace = os.environ.get("TEMPORAL_NAMESPACE", "default").strip()
    task_queue = os.environ.get("NEXCRAFT_TASK_QUEUE", "nexcraft-jobs").strip()

    fedsql, provider = build_postgres_fedsql_client()
    configure_worker(fedsql=fedsql, store=NullResultStore())
    logger.info(
        "GenieML FedSQL worker: source_id=%s target=%s queue=%s",
        default_source_id(),
        target,
        task_queue,
    )

    client = await Client.connect(target, namespace=namespace)
    # Run UNSANDBOXED — same choice as the ontology pipeline worker. The nexcraft
    # workflow bodies are deterministic by construction (only model_validate +
    # workflow.execute_activity, no time/random/IO). The default workflow sandbox
    # re-imports workflow modules to validate them, which fails on native C
    # extensions that nexcraft_jobs pulls in at load time (duckdb's
    # _duckdb._sqltypes; cf. ontology worker's pydantic_core note).
    worker = Worker(
        client,
        task_queue=task_queue,
        workflows=list(NEXCRAFT_RECIPE_WORKFLOWS),
        activities=list(NEXCRAFT_RECIPE_ACTIVITIES),
        workflow_runner=UnsandboxedWorkflowRunner(),
    )
    logger.info("Worker running on queue %s — Ctrl-C to stop.", task_queue)
    try:
        await worker.run()  # runs until cancelled
    finally:
        await provider.close()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
