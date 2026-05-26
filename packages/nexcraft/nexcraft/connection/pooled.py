"""ConnectionProvider that loads ConnectionDetails from a ManagementStore,
validates tenant against the request context, resolves credentials via a
SecretResolver, and hands out pooled driver connections per source_id.

Pool sizing is supplied externally via a ``PoolConfigProvider`` keyed by
source ``kind`` (with optional per-source overrides), so operators can tune
"how many Postgres connections" without touching the management DB.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Mapping, Protocol, runtime_checkable

from nexcraft.connection.management import (
    ConnectionDetails,
    ManagementStore,
    NullSecretResolver,
    SecretResolver,
)
from nexcraft.connection.pool_config import PoolConfig, PoolConfigProvider, StaticPoolConfig
from nexcraft.core.context import QueryContext
from nexcraft.core.descriptors import ConnectionHandle
from nexcraft.errors import AuthenticationError, ConfigurationError


@dataclass
class PooledConnectionHandle(ConnectionHandle):
    """ConnectionHandle returned by ``PooledConnectionProvider``.

    Carries the underlying driver object as ``raw`` and a back-reference to
    the pool slot so ``provider.release(handle)`` can return it cleanly.
    Executor implementations narrow this via subclasses (e.g.
    ``PostgresPooledHandle`` exposes a typed ``raw: asyncpg.Connection``).
    """

    raw: object = None
    _pool_id: str = ""


@runtime_checkable
class DriverPool(Protocol):
    """Per-source pool that hands out + reclaims live driver connections."""

    @property
    def kind(self) -> str: ...

    async def acquire(self, ctx: QueryContext) -> PooledConnectionHandle: ...

    async def release(self, handle: PooledConnectionHandle) -> None: ...

    async def close(self) -> None: ...


@runtime_checkable
class DriverPoolFactory(Protocol):
    """Builds a ``DriverPool`` for one source from its details + secrets + sizing."""

    async def create(
        self,
        *,
        details: ConnectionDetails,
        secrets: Mapping[str, str],
        pool_config: PoolConfig,
    ) -> DriverPool: ...


class PooledConnectionProvider:
    """Loads ConnectionDetails on demand and pools connections per source_id.

    Cooperative parts:
      - ``store``: the host system's connections table (Genie-style).
      - ``factories``: maps ``kind`` → ``DriverPoolFactory`` (one per backend).
      - ``pool_config``: external sizing (per-kind defaults, per-source overrides).
      - ``secrets``: resolves opaque ``secret_ref`` strings to driver fields.

    Lifecycle:
      - First ``acquire`` for a source loads its details, validates tenant,
        resolves secrets, then asks the matching factory for a ``DriverPool``.
      - Subsequent acquires for the same source reuse the cached pool.
      - ``close()`` drains every cached pool.
    """

    def __init__(
        self,
        *,
        store: ManagementStore,
        factories: Mapping[str, DriverPoolFactory],
        pool_config: PoolConfigProvider | None = None,
        secrets: SecretResolver | None = None,
    ) -> None:
        self._store = store
        self._factories = dict(factories)
        self._pool_config = pool_config or StaticPoolConfig()
        self._secrets = secrets or NullSecretResolver()

        self._pools: dict[str, DriverPool] = {}
        # Per-source lock so concurrent first-acquires for the same source
        # don't build two pools (would leak driver connections).
        self._build_locks: dict[str, asyncio.Lock] = {}
        self._build_locks_guard = asyncio.Lock()

    async def acquire(self, source_id: str, ctx: QueryContext) -> ConnectionHandle:
        details = await self._store.get_connection_details(source_id)

        if details.tenant_id != ctx.tenant_id:
            # Tenant boundary violation. Treat as auth failure: a token with
            # tenant=A must never reach a connection scoped to tenant=B.
            raise AuthenticationError(
                f"Tenant mismatch for source_id={source_id!r}: "
                f"connection.tenant_id={details.tenant_id!r}, "
                f"ctx.tenant_id={ctx.tenant_id!r}"
            )

        pool = await self._get_or_build_pool(details)
        return await pool.acquire(ctx)

    async def release(self, handle: ConnectionHandle) -> None:
        if not isinstance(handle, PooledConnectionHandle):
            return
        pool = self._pools.get(handle._pool_id)
        if pool is None:
            return
        await pool.release(handle)

    async def close(self) -> None:
        pools = list(self._pools.values())
        self._pools.clear()
        for pool in pools:
            try:
                await pool.close()
            except Exception:
                # Closing one pool must not block closing the others.
                pass

    async def _get_or_build_pool(self, details: ConnectionDetails) -> DriverPool:
        existing = self._pools.get(details.source_id)
        if existing is not None:
            return existing

        async with self._build_locks_guard:
            lock = self._build_locks.setdefault(details.source_id, asyncio.Lock())

        async with lock:
            existing = self._pools.get(details.source_id)
            if existing is not None:
                return existing

            factory = self._factories.get(details.kind)
            if factory is None:
                raise ConfigurationError(
                    f"No DriverPoolFactory registered for kind={details.kind!r} "
                    f"(source_id={details.source_id!r})"
                )

            secrets = (
                await self._secrets.resolve(
                    details.secret_ref,
                    source_id=details.source_id,
                    tenant_id=details.tenant_id,
                )
                if details.secret_ref
                else {}
            )

            sizing = self._pool_config.get(
                kind=details.kind, source_id=details.source_id
            )
            pool = await factory.create(
                details=details, secrets=secrets, pool_config=sizing
            )
            self._pools[details.source_id] = pool
            return pool


__all__ = [
    "DriverPool",
    "DriverPoolFactory",
    "PooledConnectionHandle",
    "PooledConnectionProvider",
]
