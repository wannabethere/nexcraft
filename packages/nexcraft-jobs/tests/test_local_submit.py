from __future__ import annotations

from typing import Any, Mapping

import pyarrow as pa
import pytest

from nexcraft.catalog.inmemory import InMemoryCatalog
from nexcraft.client import FedSQLClient
from nexcraft.connection.static import StaticConnectionProvider
from nexcraft.core.descriptors import ConnectionHandle, SourceDescriptor
from nexcraft.executors.memory import MemoryExecutor
from nexcraft.router import Router

from nexcraft_jobs.context import JobContext
from nexcraft_jobs.recipe import ResultStore
from nexcraft_jobs.runtime.local import LocalRuntime
from nexcraft_jobs.types import ComputeResult, ResultRef


class ProbeRecipe:
    name = "probe"
    version = "v1"

    def validate(self, params: Mapping[str, Any]) -> None:
        if "bad" in params:
            raise ValueError("bad param")

    async def extract(self, params, ctx, fedsql):
        return {}

    async def compute(self, inputs, params, ctx):
        con = ctx._duckdb
        row = con.execute("SELECT 1 AS x").fetchone()
        assert row is not None
        return ComputeResult(
            primary=pa.table({"x": [row[0]]}),
            metadata={"recipe": self.name},
        )

    async def persist(self, result, params, ctx, store: ResultStore) -> ResultRef:
        return await store.finalize(ctx, result, params)


@pytest.fixture
def mem_client() -> FedSQLClient:
    executor = MemoryExecutor()
    source = SourceDescriptor(
        source_id="mem",
        kind="memory",
        display_name="Memory",
        tenant_id="default",
        config={},
    )
    catalog = InMemoryCatalog({"mem": source})
    router = Router(
        catalog=catalog,
        connection_provider=StaticConnectionProvider(
            {"mem": ConnectionHandle(source_id="mem", kind="memory")}
        ),
        executors={"memory": executor},
    )
    return FedSQLClient(router)


@pytest.mark.asyncio
async def test_local_runtime_submit_returns_ref(mem_client: FedSQLClient) -> None:
    """LocalRuntime submits a recipe and returns a ResultRef with the right job_id."""
    jc = JobContext(tenant_id="default", job_id="job-1")
    runtime = LocalRuntime(mem_client)
    ref = await runtime.submit(ProbeRecipe(), {}, jc)
    assert ref.job_id == "job-1"


@pytest.mark.asyncio
async def test_compute_receives_extracted_inputs(mem_client: FedSQLClient) -> None:
    """Recipe.compute() gets the extract dict and a DuckDB conn with inputs registered."""

    class ExtractCheckRecipe:
        name = "extract_check"
        version = "v1"

        def validate(self, params: Mapping[str, Any]) -> None:
            return None

        async def extract(self, params, ctx, fedsql):
            return {"items": pa.table({"id": [1, 2, 3], "val": [10.0, 20.0, 30.0]})}

        async def compute(self, inputs, params, ctx):
            assert "items" in inputs, "inputs dict must include 'items'"
            con = ctx._duckdb
            row = con.execute("SELECT SUM(val) FROM items").fetchone()
            assert row is not None and row[0] == 60.0
            return ComputeResult(primary=pa.table({"total": [row[0]]}))

        async def persist(self, result, params, ctx, store):
            return await store.finalize(ctx, result, params)

    jc = JobContext(tenant_id="default", job_id="job-inputs")
    ref = await LocalRuntime(mem_client).submit(ExtractCheckRecipe(), {}, jc)
    assert ref.job_id == "job-inputs"
