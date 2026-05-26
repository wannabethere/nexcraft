"""Dedicated Temporal worker for Cornerstone ODBC multi-hop + DuckDB combine.

Registers only ``CornerstoneMultiHopWorkflow`` and its activities on task queue
``nexcraft-cornerstone-multihop``. The main nexcraft recipe worker also includes
these definitions when using ``TemporalRuntime`` defaults.

Usage (from nexcraft workspace root, Temporal listening on localhost:7233)::

    uv run python packages/nexcraft-jobs/examples/worker_cornerstone_multihop.py
"""

from __future__ import annotations

import asyncio
import logging

MULTIHOP_TASK_QUEUE = "nexcraft-cornerstone-multihop"


async def main() -> None:
    logging.basicConfig(level=logging.INFO)
    from temporalio.client import Client
    from temporalio.worker import Worker

    from nexcraft_jobs.runtime.temporal_worker_bundle import (
        NEXCRAFT_MULTIHOP_ACTIVITIES,
        NEXCRAFT_MULTIHOP_WORKFLOWS,
    )

    client = await Client.connect("localhost:7233")
    worker = Worker(
        client,
        task_queue=MULTIHOP_TASK_QUEUE,
        workflows=list(NEXCRAFT_MULTIHOP_WORKFLOWS),
        activities=list(NEXCRAFT_MULTIHOP_ACTIVITIES),
    )
    logging.info("Cornerstone multihop worker on queue %s", MULTIHOP_TASK_QUEUE)
    await worker.run()


if __name__ == "__main__":
    asyncio.run(main())
