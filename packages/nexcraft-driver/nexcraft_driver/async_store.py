"""Pluggable async-query state store.

The Flight server's submit / status / fetch / cancel handlers delegate to an
`AsyncQueryStore`. v0.0.1 ships an in-process impl backed by `asyncio.Task` +
local Parquet under a configurable directory. Swapping to Temporal later is a
new class implementing the same four-method interface — no Flight protocol or
client changes.
"""
from __future__ import annotations

import asyncio
import shutil
import traceback
import uuid
from abc import ABC, abstractmethod
from collections.abc import AsyncIterator
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq

from nexcraft.client import FedSQLClient
from nexcraft.core.context import QueryContext

from nexcraft_driver.types import (
    QueryHandle,
    QueryState,
    QueryStatus,
    SubmitRequest,
    now_utc,
)


class AsyncQueryStore(ABC):
    """Four-method interface shared by every async backend."""

    @abstractmethod
    async def submit(self, req: SubmitRequest) -> QueryHandle: ...

    @abstractmethod
    async def status(self, handle: QueryHandle) -> QueryStatus: ...

    @abstractmethod
    async def fetch(self, handle: QueryHandle) -> AsyncIterator[pa.RecordBatch]: ...

    @abstractmethod
    async def cancel(self, handle: QueryHandle) -> None: ...

    async def aclose(self) -> None:
        """Optional shutdown hook for backends that hold resources."""
        return None


class InProcessAsyncQueryStore(AsyncQueryStore):
    """Runs each submission as an asyncio.Task, spools the result to local
    Parquet, and tracks state in a dict. Single-process only — restarts lose
    state. Fine for local dev and the demo; swap for Temporal in prod."""

    def __init__(self, fedsql: FedSQLClient, *, spool_dir: Path | str = "_async_results") -> None:
        self._fedsql = fedsql
        self._spool = Path(spool_dir)
        self._spool.mkdir(parents=True, exist_ok=True)
        self._lock = asyncio.Lock()
        self._statuses: dict[str, QueryStatus] = {}
        self._tasks: dict[str, asyncio.Task] = {}

    async def submit(self, req: SubmitRequest) -> QueryHandle:
        query_id = uuid.uuid4().hex
        handle = QueryHandle(query_id=query_id, submitted_at=now_utc())
        async with self._lock:
            self._statuses[query_id] = QueryStatus(
                query_id=query_id, state=QueryState.PENDING,
            )
        self._tasks[query_id] = asyncio.create_task(self._run(query_id, req))
        return handle

    async def status(self, handle: QueryHandle) -> QueryStatus:
        async with self._lock:
            st = self._statuses.get(handle.query_id)
        if st is None:
            return QueryStatus(query_id=handle.query_id, state=QueryState.EXPIRED,
                               error_class="UnknownHandle",
                               error_message="query_id not found (expired or never submitted)")
        return st

    async def fetch(self, handle: QueryHandle) -> AsyncIterator[pa.RecordBatch]:
        st = await self.status(handle)
        if st.state is not QueryState.SUCCEEDED:
            raise RuntimeError(
                f"query {handle.query_id} not ready (state={st.state.value})"
            )
        if not st.result_uri:
            raise RuntimeError(f"query {handle.query_id} has no result_uri")
        # Yield Parquet row groups as RecordBatches.
        pf = pq.ParquetFile(st.result_uri)
        for i in range(pf.num_row_groups):
            tbl = pf.read_row_group(i)
            for batch in tbl.to_batches():
                yield batch

    async def cancel(self, handle: QueryHandle) -> None:
        task = self._tasks.get(handle.query_id)
        if task and not task.done():
            task.cancel()
        async with self._lock:
            st = self._statuses.get(handle.query_id)
            if st and st.state in (QueryState.PENDING, QueryState.RUNNING):
                st.state = QueryState.CANCELLED
                st.completed_at = now_utc()

    async def aclose(self) -> None:
        for t in self._tasks.values():
            if not t.done():
                t.cancel()
        # Best-effort spool cleanup is left for an operator sweeper; in-process
        # store keeps Parquet files until the directory is purged.

    # --- internals --------------------------------------------------------

    async def _run(self, query_id: str, req: SubmitRequest) -> None:
        async with self._lock:
            self._statuses[query_id].state = QueryState.RUNNING
            self._statuses[query_id].started_at = now_utc()
        out_path = self._spool / f"{query_id}.parquet"
        try:
            qctx = QueryContext(tenant_id=req.tenant_id, query_id=query_id)
            table = await self._fedsql.execute_to_table(req.source_id, req.sql, qctx)
            pq.write_table(table, out_path)
            async with self._lock:
                st = self._statuses[query_id]
                st.state = QueryState.SUCCEEDED
                st.completed_at = now_utc()
                st.rows = int(table.num_rows)
                st.bytes = int(out_path.stat().st_size)
                st.result_uri = str(out_path)
        except asyncio.CancelledError:
            async with self._lock:
                st = self._statuses[query_id]
                st.state = QueryState.CANCELLED
                st.completed_at = now_utc()
            if out_path.exists():
                out_path.unlink()
            raise
        except Exception as exc:  # noqa: BLE001
            async with self._lock:
                st = self._statuses[query_id]
                st.state = QueryState.FAILED
                st.completed_at = now_utc()
                st.error_class = type(exc).__name__
                st.error_message = str(exc) or "".join(traceback.format_exception_only(exc))
            if out_path.exists():
                out_path.unlink()

    def purge_spool(self) -> None:
        """Manual cleanup helper — drops all spooled Parquet files. Test-only."""
        if self._spool.exists():
            shutil.rmtree(self._spool)
            self._spool.mkdir(parents=True, exist_ok=True)
