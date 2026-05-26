from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator, AsyncIterator, Awaitable, Callable
from datetime import datetime, timezone
from typing import Any, Optional, Union

import pyarrow as pa

from nexcraft.core.context import QueryContext
from nexcraft.errors import BudgetExceededError, CancelledError
from nexcraft.errors import TimeoutError as NexcraftTimeoutError

_SENTINEL_DONE = object()


async def stream_from_batches(
    batches: list[pa.RecordBatch],
) -> AsyncGenerator[pa.RecordBatch, None]:
    for b in batches:
        yield b


class CancellableArrowStream:
    def __init__(
        self,
        producer: AsyncGenerator[pa.RecordBatch, None],
        ctx: QueryContext,
        *,
        queue_size: int = 4,
        on_cancel: Optional[Callable[[], Awaitable[None]]] = None,
    ) -> None:
        self._producer = producer
        self._ctx = ctx
        self._queue: asyncio.Queue[Union[pa.RecordBatch, BaseException, object]] = asyncio.Queue(
            maxsize=queue_size
        )
        self._on_cancel = on_cancel
        self._rows_seen = 0
        self._bytes_seen = 0
        self._pump_task: asyncio.Task[None] | None = None
        self._schema: pa.Schema | None = None

    @property
    def schema(self) -> pa.Schema:
        if self._schema is None:
            raise RuntimeError("schema unavailable until first batch is processed")
        return self._schema

    def __aiter__(self) -> AsyncIterator[pa.RecordBatch]:
        return self._gen()

    async def _gen(self) -> AsyncGenerator[pa.RecordBatch, None]:
        self._pump_task = asyncio.create_task(self._pump())
        try:
            while True:
                item = await self._next_item()
                if item is _SENTINEL_DONE:
                    return
                if isinstance(item, BaseException):
                    raise item
                yield item  # type: ignore[misc]
        finally:
            await self._cleanup()

    async def _pump(self) -> None:
        try:
            async for batch in self._producer:
                if self._should_stop():
                    await self._queue.put(self._stop_reason())
                    return
                self._rows_seen += batch.num_rows
                self._bytes_seen += int(batch.nbytes)
                if self._schema is None:
                    self._schema = batch.schema
                if self._budget_violated():
                    await self._queue.put(self._budget_error())
                    return
                await self._queue.put(batch)
            await self._queue.put(_SENTINEL_DONE)
        except BaseException as e:
            await self._queue.put(e)

    async def _next_item(self) -> Any:
        deadline = self._ctx.deadline
        if deadline is None:
            return await self._queue.get()
        timeout = (deadline - datetime.now(timezone.utc)).total_seconds()
        if timeout <= 0:
            return NexcraftTimeoutError("Query deadline expired before next batch")
        try:
            return await asyncio.wait_for(self._queue.get(), timeout)
        except asyncio.TimeoutError:
            return NexcraftTimeoutError("Query deadline expired waiting for batch")

    def _should_stop(self) -> bool:
        if self._ctx.cancel.is_set():
            return True
        if self._ctx.deadline and datetime.now(timezone.utc) >= self._ctx.deadline:
            return True
        return False

    def _stop_reason(self) -> BaseException:
        if self._ctx.cancel.is_set():
            return CancelledError("Query cancelled via QueryContext.cancel")
        return NexcraftTimeoutError("Query deadline exceeded during streaming")

    def _budget_violated(self) -> bool:
        if self._ctx.max_rows is not None and self._rows_seen > self._ctx.max_rows:
            return True
        if self._ctx.max_bytes is not None and self._bytes_seen > self._ctx.max_bytes:
            return True
        return False

    def _budget_error(self) -> BudgetExceededError:
        if self._ctx.max_rows is not None and self._rows_seen > self._ctx.max_rows:
            return BudgetExceededError(
                "Row budget exceeded",
                budget_kind="rows",
                limit=int(self._ctx.max_rows),
                observed=self._rows_seen,
            )
        return BudgetExceededError(
            "Byte budget exceeded",
            budget_kind="bytes",
            limit=int(self._ctx.max_bytes or 0),
            observed=self._bytes_seen,
        )

    async def _cleanup(self) -> None:
        if self._pump_task and not self._pump_task.done():
            self._pump_task.cancel()
            try:
                await self._pump_task
            except asyncio.CancelledError:
                pass
        if self._on_cancel:
            try:
                await self._on_cancel()
            except Exception:
                pass
