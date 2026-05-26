"""
Example: analytical recipe executed with LocalRuntime (no Temporal).

Usage (from repo root):
    python examples/02_recipe_local_runtime.py
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from nexcraft.core.context import QueryContext

from demo_kit import DEMO_SOURCE_ID, DEMO_TENANT, RevenueByRegionRecipe, build_demo_local_runtime
from nexcraft_jobs.context import JobContext


async def main() -> None:
    runtime = build_demo_local_runtime()
    job_ctx = JobContext(
        tenant_id=DEMO_TENANT,
        job_id="example-job-01",
        query=QueryContext(tenant_id=DEMO_TENANT, query_id="example-query-recipe"),
        memory_budget="512MB",
        cpu_budget=2,
    )
    ref = await runtime.submit(
        RevenueByRegionRecipe(),
        params={"source_id": DEMO_SOURCE_ID},
        ctx=job_ctx,
    )
    print("ResultRef:", ref)


if __name__ == "__main__":
    asyncio.run(main())
