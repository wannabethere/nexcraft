"""
Minimal Temporal worker for the demo recipe (pairs with 03_temporal_submit_sketch.py).

Terminal A:
    export TEMPORAL_HOST=localhost:7233
    export TEMPORAL_NAMESPACE=default
    export TEMPORAL_TASK_QUEUE=nexcraft-recipes
    python examples/run_demo_worker.py

Terminal B:
    export TEMPORAL_HOST=localhost:7233
    export TEMPORAL_TASK_QUEUE=nexcraft-recipes
    export NEXCRAFT_STAGING_ROOT=/tmp/nexcraft-staging
    python examples/03_temporal_submit_sketch.py
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from temporalio.client import Client
from temporalio.worker import Worker

from demo_kit import RevenueByRegionRecipe, build_demo_client
from nexcraft_jobs.runtime.registry import GLOBAL_REGISTRY
from nexcraft_jobs.runtime.temporal_worker_bundle import (
    NEXCRAFT_RECIPE_ACTIVITIES,
    NEXCRAFT_RECIPE_WORKFLOWS,
)
from nexcraft_jobs.runtime.worker_config import configure_worker


async def _run() -> None:
    host = os.environ["TEMPORAL_HOST"]
    namespace = os.environ.get("TEMPORAL_NAMESPACE", "default")
    task_queue = os.environ.get("TEMPORAL_TASK_QUEUE", "nexcraft-recipes")

    configure_worker(fedsql=build_demo_client(), store=None)
    GLOBAL_REGISTRY.register(RevenueByRegionRecipe())

    client = await Client.connect(host, namespace=namespace)
    worker = Worker(
        client,
        task_queue=task_queue,
        workflows=list(NEXCRAFT_RECIPE_WORKFLOWS),
        activities=list(NEXCRAFT_RECIPE_ACTIVITIES),
    )
    print(f"Nexcraft demo worker listening on queue={task_queue!r} namespace={namespace!r}")
    await worker.run()


def main() -> None:
    if not os.environ.get("TEMPORAL_HOST"):
        print("Set TEMPORAL_HOST (e.g. localhost:7233).", file=sys.stderr)
        raise SystemExit(1)
    asyncio.run(_run())


if __name__ == "__main__":
    main()
