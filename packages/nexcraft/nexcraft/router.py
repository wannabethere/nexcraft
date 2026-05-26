from __future__ import annotations

from collections.abc import AsyncIterator

import pyarrow as pa

from nexcraft.core import Catalog, ConnectionHandle, ConnectionProvider, QueryContext, SourceExecutor
from nexcraft.errors import ConfigurationError


class Router:
    def __init__(
        self,
        *,
        catalog: Catalog,
        connection_provider: ConnectionProvider,
        executors: dict[str, SourceExecutor],
    ) -> None:
        self._catalog = catalog
        self._connection_provider = connection_provider
        self._executors = dict(executors)

    async def describe(self, source_id: str, sql: str, ctx: QueryContext) -> pa.Schema:
        desc = await self._catalog.get_source(source_id)
        executor = self._executor_for(desc.kind)
        handle = await self._connection_provider.acquire(source_id, ctx)
        try:
            self._ensure_kind(handle, executor.kind)
            return await executor.describe(sql, ctx, handle)
        finally:
            await self._safe_release(handle)

    async def execute(
        self,
        source_id: str,
        sql: str,
        ctx: QueryContext,
    ) -> AsyncIterator[pa.RecordBatch]:
        desc = await self._catalog.get_source(source_id)
        executor = self._executor_for(desc.kind)
        handle = await self._connection_provider.acquire(source_id, ctx)
        try:
            self._ensure_kind(handle, executor.kind)
            async for batch in executor.execute(sql, ctx, handle):
                yield batch
        finally:
            await self._safe_release(handle)

    def _executor_for(self, kind: str) -> SourceExecutor:
        try:
            return self._executors[kind]
        except KeyError as e:
            raise ConfigurationError(f"No executor registered for kind={kind!r}") from e

    def _ensure_kind(self, handle: ConnectionHandle, kind: str) -> None:
        if handle.kind != kind:
            raise ConfigurationError(
                f"Connection handle kind mismatch: handle.kind={handle.kind!r}, executor.kind={kind!r}"
            )

    async def _safe_release(self, handle: ConnectionHandle) -> None:
        release = getattr(self._connection_provider, "release", None)
        if release is None:
            return
        try:
            await release(handle)
        except Exception:
            # Releasing a handle must never mask the in-flight exception or
            # break the streaming contract; log via the caller's facilities.
            pass
