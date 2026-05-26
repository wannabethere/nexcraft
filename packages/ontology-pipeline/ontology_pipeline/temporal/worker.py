"""Temporal worker bootstrap for the ontology-pipeline workflow.

Run via:

    python -m ontology_pipeline.temporal.worker \\
        --temporal-target localhost:7233 \\
        --task-queue ontology-pipeline-default \\
        --namespace default

The worker registers:
  - `OntologyIngestionWorkflow` (workflow_type='ontology_ingestion')
  - All `ontology.*` activities

A YAML job spec submitted via the `nexcraft-yaml-job` CLI (from the
`nexcraft-jobs` package — note the singular `job` in the binary name) with
`workflow_type: ontology_ingestion` + matching `task_queue` will land here.

For local development you can also keep the worker alive in a separate
terminal and submit one-off jobs from another shell.
"""
from __future__ import annotations

import asyncio
import logging
import os
import signal
from concurrent.futures import ThreadPoolExecutor
from typing import Any

logger = logging.getLogger(__name__)


async def build_worker(
    *,
    temporal_target: str,
    namespace: str = "default",
    task_queue: str = "ontology-pipeline-default",
    activity_pool_size: int = 16,
) -> tuple[Any, ThreadPoolExecutor]:
    """Connect to Temporal and build a Worker registered for our workflow + activities.

    Returns `(worker, executor)`. The executor is the activity-pool the worker
    uses to run sync activities; the caller is responsible for shutting it
    down (the long-running `run_worker()` does this for you in a `finally`
    block).

    Our activity bodies are SYNC because they call blocking I/O (psycopg
    for source samples, pandas for CSV reads, OpenAI / DeepSeek HTTP for
    LLM enrichment, SQLAlchemy for ontology-store writes). Temporal requires
    either `async def` activities OR a configured `activity_executor` for
    sync ones — we pick the latter so the bodies stay synchronous (more
    readable, easier to call from in-process pipeline code too).

    Args:
        temporal_target: Temporal frontend `host:port`.
        namespace: Temporal namespace. Default "default".
        task_queue: Task queue name. Default "ontology-pipeline-default".
        activity_pool_size: max in-flight sync activities. Must be ≥
            `per_table_concurrency` from the YAML, plus headroom for the
            post-pass activities. Default 16.
    """
    from temporalio.client import Client
    from temporalio.contrib.pydantic import pydantic_data_converter
    from temporalio.worker import UnsandboxedWorkflowRunner, Worker

    from ontology_pipeline.temporal.activities import ACTIVITIES
    from ontology_pipeline.temporal.workflows import OntologyIngestionWorkflow

    if OntologyIngestionWorkflow is None:
        raise RuntimeError(
            "OntologyIngestionWorkflow could not be built; temporalio missing or broken."
        )
    if ACTIVITIES is None:
        raise RuntimeError(
            "Temporal activities are not registered; install [temporal] extra."
        )

    # The default JSON converter can't serialize `pathlib.Path` (used inside
    # `LocalFilesSource` for schema_sql / data_dir / manifest, and in
    # `OutputConfig.base_dir`). Pydantic's `model_dump(mode='json')` handles
    # Path, datetime, UUID, etc. natively — wire it on both ends.
    #
    # The submitter (nexcraft-yaml-job) also needs to use the same data
    # converter when it starts the workflow; we hand it via the same env var
    # convention `TEMPORAL_DATA_CONVERTER=pydantic` if the CLI supports it.
    # Otherwise the workflow input arrives as raw dicts, which `OntologyIngestionInput.model_validate(...)`
    # at the top of `run()` already tolerates.
    client = await Client.connect(
        temporal_target,
        namespace=namespace,
        data_converter=pydantic_data_converter,
    )
    executor = ThreadPoolExecutor(
        max_workers=activity_pool_size,
        thread_name_prefix="ontology-pipeline-activity",
    )
    # The workflow body is deterministic by construction (no time, no random,
    # no DB / network — only `OntologyIngestionInput.model_validate(...)` and
    # `workflow.execute_activity(...)`). Going unsandboxed sidesteps the
    # default sandbox's re-import semantics, which trip on pydantic_core's
    # C extension ("cannot load module more than once per process").
    worker = Worker(
        client,
        task_queue=task_queue,
        workflows=[OntologyIngestionWorkflow],
        activities=ACTIVITIES,
        activity_executor=executor,
        workflow_runner=UnsandboxedWorkflowRunner(),
    )
    logger.info(
        "Worker built: target=%s namespace=%s task_queue=%s activity_pool=%d",
        temporal_target, namespace, task_queue, activity_pool_size,
    )
    return worker, executor


async def run_worker(
    *,
    temporal_target: str,
    namespace: str = "default",
    task_queue: str = "ontology-pipeline-default",
    activity_pool_size: int = 16,
) -> None:
    """Long-running worker. Handles SIGINT / SIGTERM for graceful shutdown.

    Shuts the activity ThreadPoolExecutor down in a `finally` block so a
    Ctrl-C doesn't leave orphan threads.
    """
    worker, executor = await build_worker(
        temporal_target=temporal_target, namespace=namespace,
        task_queue=task_queue, activity_pool_size=activity_pool_size,
    )
    stop_event = asyncio.Event()

    # Use `add_signal_handler` on the running loop so Ctrl-C wakes the
    # awaiter on `stop_event.wait()` cleanly. The older `signal.signal`
    # API runs the handler in the main thread but doesn't poke the
    # asyncio event loop — on Python 3.13 / macOS this results in the
    # worker noticing SIGINT but never actually exiting until SIGTERM /
    # SIGKILL hits.
    loop = asyncio.get_running_loop()

    def _stop(signum: int) -> None:
        logger.info("Received signal %s; shutting worker down", signum)
        stop_event.set()

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, _stop, sig)
        except (NotImplementedError, RuntimeError):
            # Windows / restricted environments — fall back to the sync API.
            try:
                signal.signal(
                    sig,
                    lambda signum, _frame: (
                        logger.info("Received signal %s (fallback handler)", signum),
                        loop.call_soon_threadsafe(stop_event.set),
                    ),
                )
            except Exception:
                pass

    try:
        async with worker:
            await stop_event.wait()
            logger.info("Stop event received; exiting worker context")
    finally:
        # Don't block forever waiting for in-flight activities. Cancel queued
        # tasks; let in-flight ones get cut off when their threads exit. The
        # worker's __aexit__ already waited for activities to finish (or
        # timed out) before returning.
        executor.shutdown(wait=False, cancel_futures=True)
        logger.info("Activity executor signaled shutdown")


def _parse_args() -> Any:
    import argparse
    p = argparse.ArgumentParser(
        prog="ontology-pipeline-temporal-worker",
        description="Run the OntologyIngestionWorkflow worker.",
    )
    p.add_argument(
        "--temporal-target",
        default=os.environ.get("TEMPORAL_TARGET", "localhost:7233"),
        help="Temporal frontend host:port. Default localhost:7233 (or $TEMPORAL_TARGET).",
    )
    p.add_argument(
        "--namespace",
        default=os.environ.get("TEMPORAL_NAMESPACE", "default"),
        help="Temporal namespace. Default 'default' (or $TEMPORAL_NAMESPACE).",
    )
    p.add_argument(
        "--task-queue",
        default=os.environ.get(
            "ONTOLOGY_PIPELINE_TASK_QUEUE", "ontology-pipeline-default",
        ),
        help="Task queue. Default 'ontology-pipeline-default'.",
    )
    p.add_argument(
        "--activity-pool-size", type=int,
        default=int(os.environ.get("ONTOLOGY_PIPELINE_ACTIVITY_POOL", "16")),
        help=(
            "Max concurrent sync activities. Must be ≥ per_table_concurrency in "
            "the YAML. Default 16 (or $ONTOLOGY_PIPELINE_ACTIVITY_POOL)."
        ),
    )
    p.add_argument(
        "--log-level", default=os.environ.get("LOG_LEVEL", "INFO"),
    )
    return p.parse_args()


def main() -> None:
    args = _parse_args()
    logging.basicConfig(
        level=getattr(logging, args.log_level.upper(), logging.INFO),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    asyncio.run(run_worker(
        temporal_target=args.temporal_target,
        namespace=args.namespace,
        task_queue=args.task_queue,
        activity_pool_size=args.activity_pool_size,
    ))


if __name__ == "__main__":
    main()
