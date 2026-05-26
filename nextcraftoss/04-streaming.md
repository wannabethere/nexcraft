# 04 — Streaming, Cancellation, Budgets

The streaming primitive is the single most important piece of code in the library. Centralizing it in `nexcraft.streaming` means every executor gets correct behavior for free, and bugs get fixed once.

## `CancellableArrowStream`

```python
from typing import AsyncIterator, AsyncGenerator, Optional
from datetime import datetime, timezone
import asyncio
import pyarrow as pa
from nexcraft.core import QueryContext
from nexcraft.errors import (
    TimeoutError, CancelledError, BudgetExceededError,
)

class CancellableArrowStream:
    """Async iterator over RecordBatches with bounded buffering, cancellation,
    deadline enforcement, and row/byte budget accounting.

    Constructed by executors; consumed by the public client.
    """

    def __init__(
        self,
        producer: AsyncGenerator[pa.RecordBatch, None],
        ctx: QueryContext,
        queue_size: int = 4,
        on_cancel: Optional["Callable[[], Awaitable[None]]"] = None,
    ):
        self._producer = producer
        self._ctx = ctx
        self._queue: asyncio.Queue[_QueueItem] = asyncio.Queue(maxsize=queue_size)
        self._on_cancel = on_cancel              # source-side cancel hook
        self._rows_seen = 0
        self._bytes_seen = 0
        self._pump_task: Optional[asyncio.Task] = None

    @property
    def schema(self) -> pa.Schema:
        ...

    async def __aiter__(self) -> AsyncIterator[pa.RecordBatch]:
        self._pump_task = asyncio.create_task(self._pump())
        try:
            while True:
                item = await self._next_item()
                if item is _SENTINEL_DONE:
                    return
                if isinstance(item, BaseException):
                    raise item
                yield item
        finally:
            await self._cleanup()

    async def _pump(self):
        """Drains producer into the queue. Stops on cancel/deadline/budget/error."""
        try:
            async for batch in self._producer:
                if self._should_stop():
                    await self._queue.put(self._stop_reason())
                    return
                self._rows_seen += batch.num_rows
                self._bytes_seen += batch.nbytes
                if self._budget_violated():
                    await self._queue.put(self._budget_error())
                    return
                await self._queue.put(batch)        # blocks → backpressure
            await self._queue.put(_SENTINEL_DONE)
        except BaseException as e:
            await self._queue.put(e)

    async def _next_item(self):
        deadline = self._ctx.deadline
        if deadline is None:
            return await self._queue.get()
        timeout = (deadline - datetime.now(timezone.utc)).total_seconds()
        if timeout <= 0:
            return TimeoutError(...)
        try:
            return await asyncio.wait_for(self._queue.get(), timeout)
        except asyncio.TimeoutError:
            return TimeoutError(...)

    def _should_stop(self) -> bool:
        if self._ctx.cancel.is_set():
            return True
        if self._ctx.deadline and datetime.now(timezone.utc) >= self._ctx.deadline:
            return True
        return False

    def _budget_violated(self) -> bool:
        if self._ctx.max_rows and self._rows_seen > self._ctx.max_rows:
            return True
        if self._ctx.max_bytes and self._bytes_seen > self._ctx.max_bytes:
            return True
        return False

    async def _cleanup(self):
        if self._pump_task and not self._pump_task.done():
            self._pump_task.cancel()
        if self._on_cancel:
            try:
                await self._on_cancel()           # source-side cancel
            except Exception:
                pass                              # best-effort
```

The shape is deliberately simple. Producer pumps into a bounded queue, consumer drains. Cancellation, deadlines, and budgets are checked between batches. Source-side cancel runs in `_cleanup` if provided.

## Backpressure

Backpressure is implicit: `Queue(maxsize=4)` blocks the producer when the consumer is slow. With a default batch size of 8K rows × N columns, four batches of buffering is small enough to bound memory and large enough to absorb transient consumer stalls.

The queue size is configurable per-call via `QueryContext.batch_size_hint` and (separately) via the executor's stream construction. Most users should leave defaults alone.

## Cancellation — three layers

Cancellation must work end-to-end. Three layers, all required:

### Layer 1 — Caller cancels asyncio task

```python
task = asyncio.create_task(consume(stream))
task.cancel()
```

`asyncio.CancelledError` propagates through the `async for` loop, the `_aiter` cleanup runs, the pump task is cancelled.

### Layer 2 — `ctx.cancel` event

```python
ctx.cancel.set()                # programmatic cancel without task cancel
```

Set by orchestration logic (e.g., a parent recipe cancels children). The pump checks `ctx.cancel.is_set()` between batches and stops.

### Layer 3 — Source-side cancel

The pump stopping doesn't help if the source is still chewing on a 5-minute query. Each executor provides `on_cancel` that issues a real cancel at the source:

| Source     | Cancel mechanism                                           |
|------------|------------------------------------------------------------|
| Postgres   | `SELECT pg_cancel_backend(pid)` on a side connection       |
| Snowflake  | `SYSTEM$CANCEL_QUERY('<query_id>')` on a side connection   |
| BigQuery   | `jobs.cancel(jobId)` REST call                             |
| Iceberg    | DataFusion plan supports cancellation; propagates cleanly  |
| Delta      | Same as Iceberg                                            |

The executor populates `on_cancel` when constructing the `CancellableArrowStream`. The connection provider must expose a side-channel mechanism for the pass-through executors that need a second connection to issue the cancel.

**This is non-negotiable.** Ghost queries are the worst class of bug: the caller thinks they cancelled, the source is still running, billing accumulates, connections are pinned. End-to-end cancel test from day one, in CI, for every executor.

## Deadlines

Absolute timestamps in `QueryContext.deadline`. The pump checks before each batch; the consumer wait is bounded by `asyncio.wait_for`. On expiry, the stream raises `TimeoutError` and `_cleanup` runs the source-side cancel.

Relative deadlines are a caller concern: `deadline = datetime.now(timezone.utc) + timedelta(seconds=30)`.

## Budgets

Two hard caps:

- `max_rows` — total rows yielded across the stream.
- `max_bytes` — total `RecordBatch.nbytes` across the stream.

Counted in the pump; if exceeded, raise `BudgetExceededError(budget_kind=..., limit=..., observed=...)` and tear down.

**Why budgets matter at TB scale.** The most common production failure for a federated SQL service is "extract was bigger than expected → OOM / disk full / runaway cost." Budgets surface this as a clean, structured error before the host process dies. Recipes in `nexcraft-jobs` always set aggressive budgets at extract time so problems are caught early with actionable messages.

## What this primitive does *not* do

- **No retries.** A retry is a new query; new query = new context = new stream. Retries are caller-side.
- **No batching/coalescing.** RecordBatches are passed through as the source produced them. Consumers that want larger batches concat themselves.
- **No materialization.** No `.to_table()`, no `.collect()`. That's a convenience method on the client (`execute_to_table`), implemented by consuming the stream.
- **No format conversion.** Arrow in, Arrow out. Pandas / Polars / NumPy conversions are caller-side.
