from __future__ import annotations

from typing import AsyncIterator, Protocol, runtime_checkable

import pyarrow as pa

from nexcraft.core.context import QueryContext
from nexcraft.core.descriptors import ConnectionHandle, SourceDescriptor


@runtime_checkable
class SourceExecutor(Protocol):
    """Runs dialect-correct SQL against one source and streams Arrow batches."""

    @property
    def kind(self) -> str: ...

    async def describe(
        self,
        sql: str,
        ctx: QueryContext,
        conn: ConnectionHandle,
    ) -> pa.Schema: ...

    def execute(
        self,
        sql: str,
        ctx: QueryContext,
        conn: ConnectionHandle,
    ) -> AsyncIterator[pa.RecordBatch]: ...


@runtime_checkable
class Catalog(Protocol):
    async def get_source(self, source_id: str) -> SourceDescriptor: ...

    async def list_sources(self, tenant_id: str | None = None) -> list[SourceDescriptor]: ...


@runtime_checkable
class ConnectionProvider(Protocol):
    async def acquire(self, source_id: str, ctx: QueryContext) -> ConnectionHandle: ...

    async def release(self, handle: ConnectionHandle) -> None:
        """Return a handle to its pool / release any per-query resources.

        Providers that hand out singleton handles (tests, static config) can
        leave this as a no-op. Pooled providers must return the handle so
        subsequent acquires can reuse it.
        """
        ...
