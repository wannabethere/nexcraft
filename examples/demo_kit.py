"""Shared demo wiring for examples (memory executor + canned sales rows)."""

from __future__ import annotations

from typing import Any, Mapping

import pyarrow as pa

from nexcraft.catalog.inmemory import InMemoryCatalog
from nexcraft.client import FedSQLClient
from nexcraft.connection.static import StaticConnectionProvider
from nexcraft.core.context import QueryContext
from nexcraft.core.descriptors import ConnectionHandle, SourceDescriptor
from nexcraft.executors.memory import MemoryExecutor
from nexcraft.router import Router

from nexcraft_jobs.context import JobContext
from nexcraft_jobs.recipe import ResultStore
from nexcraft_jobs.runtime.local import LocalRuntime
from nexcraft_jobs.types import ComputeResult, ResultRef

DEMO_SOURCE_ID = "demo_wh"
DEMO_TENANT = "tenant_demo"

_SALES_BATCH = pa.RecordBatch.from_arrays(
    [
        pa.array(["east", "east", "west"], type=pa.string()),
        pa.array([100.0, 50.0, 200.0], type=pa.float64()),
    ],
    names=["region", "revenue"],
)


def build_demo_client() -> FedSQLClient:
    """In-memory \"warehouse\" with two SQL aliases over the same snapshot."""
    replies = {
        "SELECT region, revenue FROM sales": [_SALES_BATCH],
        "SELECT region, revenue FROM sales ORDER BY region": [_SALES_BATCH],
    }
    executor = MemoryExecutor(replies=replies)
    descriptor = SourceDescriptor(
        source_id=DEMO_SOURCE_ID,
        kind="memory",
        display_name="Demo warehouse",
        tenant_id=DEMO_TENANT,
        config={},
    )
    catalog = InMemoryCatalog({DEMO_SOURCE_ID: descriptor})
    handles = {DEMO_SOURCE_ID: ConnectionHandle(source_id=DEMO_SOURCE_ID, kind="memory")}
    router = Router(
        catalog=catalog,
        connection_provider=StaticConnectionProvider(handles),
        executors={"memory": executor},
    )
    return FedSQLClient(router)


class RevenueByRegionRecipe:
    """Registered by the demo Temporal worker; keep name/version stable for SubmitJobPayload."""

    name = "revenue_by_region"
    version = "v1"

    def validate(self, params: Mapping[str, Any]) -> None:
        if "source_id" not in params:
            raise ValueError("params must include source_id")

    async def extract(
        self,
        params: Mapping[str, Any],
        ctx: JobContext,
        fedsql: FedSQLClient,
    ) -> Mapping[str, pa.Table]:
        source_id = str(params["source_id"])
        sql = str(params.get("extract_sql", "SELECT region, revenue FROM sales"))
        table = await fedsql.execute_to_table(source_id, sql, ctx.query)
        return {"sales": table}

    async def compute(self, params: Mapping[str, Any], ctx: JobContext, con) -> ComputeResult:
        rel = con.execute(
            """
            SELECT region, SUM(revenue)::DOUBLE AS revenue
            FROM sales
            GROUP BY region
            ORDER BY region
            """
        )
        rows = rel.fetchall()
        return ComputeResult(recipe_name=self.name, metrics={"row_count": len(rows), "aggregate": rows})

    async def persist(
        self,
        result: ComputeResult,
        params: Mapping[str, Any],
        ctx: JobContext,
        store: ResultStore,
    ) -> ResultRef:
        return await store.finalize(ctx, result, params)


def build_demo_local_runtime() -> LocalRuntime:
    return LocalRuntime(build_demo_client())
