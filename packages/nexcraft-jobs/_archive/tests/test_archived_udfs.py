"""Archived tests for the deprecated DuckDB-UDF lane.

These tests covered the legacy `nexcraft_jobs.compute.udfs` package, which has
been moved to `_archive/udfs/` and is no longer wired into the runtime. The
whole module is force-skipped so pytest never runs them; we keep the source so
git history of the deprecated behaviour stays one click away.

To re-enable for a one-off check, drop the `pytestmark = pytest.mark.skip(...)`
line below, copy `_archive/udfs/` back to `nexcraft_jobs/compute/udfs/`, and
re-add the `register_analytical_udfs` calls in `runtime/local.py` and
`runtime/temporal_staged_activities.py`. Don't ship that combination.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Mapping

import pyarrow as pa
import pytest

pytestmark = pytest.mark.skip(reason="Archived: DuckDB-UDF lane retired in favour of dstools.")


# The imports below intentionally use the archived path. They will fail to
# resolve unless the udfs/ tree is reinstated; that's fine because pytestmark
# short-circuits the whole module before collection runs the tests.


@pytest.mark.asyncio
async def test_local_runtime_submit_registers_udfs(mem_client) -> None:
    from nexcraft_jobs.context import JobContext
    from nexcraft_jobs.runtime.local import LocalRuntime
    jc = JobContext(tenant_id="default", job_id="job-1")
    runtime = LocalRuntime(mem_client)
    ref = await runtime.submit(_ProbeRecipe(), {}, jc)
    assert ref.job_id == "job-1"


@pytest.mark.asyncio
async def test_ema_udf_available(mem_client) -> None:
    from nexcraft_jobs.context import JobContext
    from nexcraft_jobs.runtime.local import LocalRuntime
    from nexcraft_jobs.types import ComputeResult

    class UdfProbeRecipe:
        name = "udf_probe"
        version = "v1"

        def validate(self, params: Mapping[str, Any]) -> None:
            return None

        async def extract(self, params, ctx, fedsql):
            return {}

        async def compute(self, inputs, params, ctx):
            con = ctx._duckdb
            tbl = pa.Table.from_arrays(
                [pa.array([[1.0, 2.0, 3.0], [10.0, 11.0]], type=pa.list_(pa.float64()))],
                names=["v"],
            )
            con.register("t", tbl)
            rel = con.execute("SELECT ema(v, 0.5) AS e FROM t")
            rows = rel.fetchall()
            return ComputeResult(
                primary=pa.table({"e": [str(r[0]) for r in rows]}),
                metadata={"ok": True},
            )

        async def persist(self, result, params, ctx, store):
            return await store.finalize(ctx, result, params)

    jc = JobContext(tenant_id="default", job_id="job-udf")
    ref = await LocalRuntime(mem_client).submit(UdfProbeRecipe(), {}, jc)
    assert ref.uri.startswith("memory://")


def test_mltools_slices_cover_sql_functions_catalog() -> None:
    from nexcraft_jobs.compute.udfs.mltools import (  # type: ignore[import-not-found]
        anomalydetection,
        group_aggregation_functions,
        movingaverages,
        operations_tools,
        timeseriesanalysis,
    )

    catalog = (
        Path(__file__).resolve().parents[2]
        / "_archive"
        / "udfs"
        / "data"
        / "sql_functions.json"
    )
    with catalog.open() as f:
        ref = set(json.load(f)["function_reference"])
    documented = (
        movingaverages.FUNCTION_NAMES
        | timeseriesanalysis.FUNCTION_NAMES
        | anomalydetection.FUNCTION_NAMES
        | group_aggregation_functions.FUNCTION_NAMES
        | operations_tools.FUNCTION_NAMES
    )
    assert documented == ref


@pytest.mark.asyncio
async def test_invoke_sql_function_calculate_sma(mem_client) -> None:
    from nexcraft_jobs.context import JobContext
    from nexcraft_jobs.runtime.local import LocalRuntime
    from nexcraft_jobs.types import ComputeResult

    class InvokeProbeRecipe:
        name = "invoke_probe"
        version = "v1"

        def validate(self, params: Mapping[str, Any]) -> None:
            return None

        async def extract(self, params, ctx, fedsql):
            return {}

        async def compute(self, inputs, params, ctx):
            con = ctx._duckdb
            payload = json.dumps(
                {
                    "p_data": [
                        {"time": "2024-01-01T00:00:00", "value": 10.0},
                        {"time": "2024-01-02T00:00:00", "value": 20.0},
                        {"time": "2024-01-03T00:00:00", "value": 15.0},
                    ],
                    "p_window_size": 2,
                    "p_group_by": "",
                }
            )
            row = con.execute(
                "SELECT invoke_sql_function(?, ?) AS r",
                ["calculate_sma", payload],
            ).fetchone()
            assert row is not None
            return ComputeResult(primary=pa.table({"r": [row[0]]}), metadata={"ok": True})

        async def persist(self, result, params, ctx, store):
            return await store.finalize(ctx, result, params)

    jc = JobContext(tenant_id="default", job_id="job-invoke")
    ref = await LocalRuntime(mem_client).submit(InvokeProbeRecipe(), {}, jc)
    assert ref.uri.startswith("memory://")


class _ProbeRecipe:
    name = "probe"
    version = "v1"

    def validate(self, params: Mapping[str, Any]) -> None:
        if "bad" in params:
            raise ValueError("bad param")

    async def extract(self, params, ctx, fedsql):
        return {}

    async def compute(self, inputs, params, ctx):
        from nexcraft_jobs.types import ComputeResult
        con = ctx._duckdb
        row = con.execute("SELECT 1 AS x").fetchone()
        return ComputeResult(primary=pa.table({"x": [row[0]]}), metadata={"recipe": self.name})

    async def persist(self, result, params, ctx, store):
        return await store.finalize(ctx, result, params)
