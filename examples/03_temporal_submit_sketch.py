"""
Sketch: start nexcraft_recipe_staged from a Temporal client.

Prerequisites:
  - Temporal server reachable (e.g. localhost:7233)
  - Worker running with NEXCRAFT_RECIPE_ACTIVITIES / NEXCRAFT_RECIPE_WORKFLOWS,
    configure_worker(...), and GLOBAL_REGISTRY.register(...) for your recipe

Usage:
    export TEMPORAL_HOST=localhost:7233
    export TEMPORAL_NAMESPACE=default
    export TEMPORAL_TASK_QUEUE=nexcraft-recipes
    export NEXCRAFT_STAGING_ROOT=/tmp/nexcraft-staging
    python examples/03_temporal_submit_sketch.py

If TEMPORAL_HOST is unset, prints instructions and exits 0.
"""

from __future__ import annotations

import asyncio
import os
import sys


async def _run() -> int:
    host = os.environ.get("TEMPORAL_HOST")
    if not host:
        print(__doc__)
        return 0

    namespace = os.environ.get("TEMPORAL_NAMESPACE", "default")
    task_queue = os.environ.get("TEMPORAL_TASK_QUEUE", "nexcraft-recipes")
    staging_root = os.environ.get("NEXCRAFT_STAGING_ROOT", "/tmp/nexcraft-staging")

    from temporalio.client import Client

    from nexcraft_jobs.runtime.temporal_payloads import SubmitJobPayload

    client = await Client.connect(host, namespace=namespace)

    payload = SubmitJobPayload(
        recipe_name="revenue_by_region",
        recipe_version="v1",
        params={"source_id": "demo_wh"},
        tenant_id="tenant_demo",
        job_id="example-temporal-01",
        query_id="example-temporal-query-01",
        staging_root=staging_root,
        memory_budget="1GB",
        cpu_budget=4,
    )

    handle = await client.start_workflow(
        "nexcraft_recipe_staged",
        args=[payload],
        id=f"nexcraft-example-{payload.job_id}",
        task_queue=task_queue,
    )
    print("Started workflow:", handle.id)
    result = await handle.result()
    print("ResultRef:", result)
    return 0


def main() -> None:
    try:
        exit_code = asyncio.run(_run())
    except Exception as exc:
        print("Temporal example failed:", exc, file=sys.stderr)
        exit_code = 1
    raise SystemExit(exit_code)


if __name__ == "__main__":
    main()
