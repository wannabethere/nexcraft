from __future__ import annotations

from collections.abc import AsyncIterator

import pyarrow as pa

from nexcraft.core.context import QueryContext
from nexcraft.core.descriptors import ConnectionHandle
from nexcraft.errors import SourceSyntaxError
from nexcraft.streaming.cancellable_stream import stream_from_batches


class MemoryExecutor:
    """Deterministic stub executor for tests (kind=`memory`)."""

    def __init__(self, *, replies: dict[str, list[pa.RecordBatch]] | None = None) -> None:
        self._replies = dict(replies) if replies else {}

    @property
    def kind(self) -> str:
        return "memory"

    async def describe(self, sql: str, ctx: QueryContext, conn: ConnectionHandle) -> pa.Schema:
        batches = self._sql_batches(sql)
        if not batches:
            return pa.schema([pa.field("_zero", pa.int32())])
        return batches[0].schema

    def execute(
        self,
        sql: str,
        ctx: QueryContext,
        conn: ConnectionHandle,
    ) -> AsyncIterator[pa.RecordBatch]:
        return stream_from_batches(self._sql_batches(sql))

    def _sql_batches(self, sql: str) -> list[pa.RecordBatch]:
        key = sql.strip()
        if key in self._replies:
            return list(self._replies[key])
        upper = key.upper()
        if upper.startswith("SELECT 1") or upper == "SELECT 1;":
            arr = pa.array([1], type=pa.int32())
            return [pa.RecordBatch.from_arrays([arr], ["one"])]
        raise SourceSyntaxError(
            f"No canned reply for SQL: {sql!r}",
            source_message="memory executor requires replies mapping",
        )
