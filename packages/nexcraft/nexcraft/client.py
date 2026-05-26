from __future__ import annotations

from collections.abc import AsyncGenerator, AsyncIterator

import pyarrow as pa

from nexcraft.core.context import QueryContext
from nexcraft.router import Router
from nexcraft.streaming.cancellable_stream import CancellableArrowStream


class FedSQLClient:
    """Thin facade over Router with cancellable streaming."""

    def __init__(self, router: Router) -> None:
        self._router = router

    async def describe(self, source_id: str, sql: str, ctx: QueryContext) -> pa.Schema:
        return await self._router.describe(source_id, sql, ctx)

    async def execute(
        self,
        source_id: str,
        sql: str,
        ctx: QueryContext,
        *,
        use_stream_guard: bool = True,
        queue_size: int = 4,
    ) -> AsyncIterator[pa.RecordBatch]:
        async def producer() -> AsyncGenerator[pa.RecordBatch, None]:
            async for batch in self._router.execute(source_id, sql, ctx):
                yield batch

        if use_stream_guard:
            wrapped = CancellableArrowStream(producer(), ctx, queue_size=queue_size)
            async for batch in wrapped:
                yield batch
        else:
            async for batch in producer():
                yield batch

    async def execute_to_table(
        self,
        source_id: str,
        sql: str,
        ctx: QueryContext,
        *,
        use_stream_guard: bool = True,
    ) -> pa.Table:
        batches: list[pa.RecordBatch] = []
        schema: pa.Schema | None = None
        async for batch in self.execute(
            source_id, sql, ctx, use_stream_guard=use_stream_guard
        ):
            if schema is None:
                schema = batch.schema
            batches.append(batch)
        if schema is None:
            return pa.table({})
        return pa.Table.from_batches(batches, schema=schema)

    async def execute_to_reader(
        self,
        source_id: str,
        sql: str,
        ctx: QueryContext,
        *,
        use_stream_guard: bool = True,
    ) -> pa.RecordBatchReader:
        """Materialize the query and expose it as a synchronous RecordBatchReader.

        Recipes (and DuckDB's ``con.register``) consume RecordBatchReader. We
        materialize the async stream here because PyArrow's reader interface is
        synchronous; staged Temporal extracts spill to Parquet so the in-memory
        cost is bounded by the per-query budgets on ``ctx``.
        """
        table = await self.execute_to_table(
            source_id, sql, ctx, use_stream_guard=use_stream_guard
        )
        return pa.RecordBatchReader.from_batches(table.schema, table.to_batches())
