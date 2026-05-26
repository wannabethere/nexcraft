from __future__ import annotations

from typing import Any, Mapping, Protocol, runtime_checkable

import pyarrow as pa

from nexcraft.client import FedSQLClient

from nexcraft_jobs.context import JobContext
from nexcraft_jobs.types import ComputeResult, ResultRef


@runtime_checkable
class ResultStore(Protocol):
    async def finalize(
        self,
        ctx: JobContext,
        result: ComputeResult,
        params: Mapping[str, Any],
    ) -> ResultRef: ...


@runtime_checkable
class Recipe(Protocol):
    """Four-phase contract aligned with Temporal activities (jobs/02-temporal.md)."""

    @property
    def name(self) -> str: ...

    @property
    def version(self) -> str: ...

    def validate(self, params: Mapping[str, Any]) -> None: ...

    async def extract(
        self,
        params: Mapping[str, Any],
        ctx: JobContext,
        fedsql: FedSQLClient,
    ) -> Mapping[str, pa.RecordBatchReader | pa.Table]:
        ...

    async def compute(
        self,
        inputs: Mapping[str, pa.RecordBatchReader | pa.Table],
        params: Mapping[str, Any],
        ctx: JobContext,
    ) -> ComputeResult:
        """Heavy lifting on the extracted inputs.

        The runtime registers ``inputs`` as DuckDB tables/views before this is
        called, and exposes the configured connection on ``ctx._duckdb``. The
        ``inputs`` mapping is supplied for recipes that prefer to read Arrow
        directly (e.g. for NumPy/SciPy-side compute).
        """

    async def persist(
        self,
        result: ComputeResult,
        params: Mapping[str, Any],
        ctx: JobContext,
        store: ResultStore,
    ) -> ResultRef:
        ...
