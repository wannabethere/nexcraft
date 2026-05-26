from __future__ import annotations

from nexcraft.core.context import QueryContext
from nexcraft.core.descriptors import ConnectionHandle
from nexcraft.errors import ConfigurationError


class StaticConnectionProvider:
    """Returns preconstructed handles (development / tests)."""

    def __init__(self, handles: dict[str, ConnectionHandle]) -> None:
        self._handles = dict(handles)

    async def acquire(self, source_id: str, ctx: QueryContext) -> ConnectionHandle:
        try:
            return self._handles[source_id]
        except KeyError as e:
            raise ConfigurationError(f"No connection handle for source_id={source_id!r}") from e

    async def release(self, handle: ConnectionHandle) -> None:
        # Static handles are singletons; nothing to release.
        return None
