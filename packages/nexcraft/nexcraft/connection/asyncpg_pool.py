"""asyncpg-backed DriverPool / DriverPoolFactory.

Lazy-imports asyncpg so the rest of nexcraft stays import-cheap. Install via
the ``postgres`` extra (``pip install 'nexcraft[postgres]'``).
"""

from __future__ import annotations

import asyncio
from typing import Any, Mapping

from nexcraft.connection.management import ConnectionDetails
from nexcraft.connection.pool_config import PoolConfig
from nexcraft.connection.pooled import (
    DriverPool,
    DriverPoolFactory,
    PooledConnectionHandle,
)
from nexcraft.core.context import QueryContext
from nexcraft.errors import (
    AuthenticationError,
    ConfigurationError,
    ConnectionError as NexcraftConnectionError,
)


def _require_asyncpg():
    try:
        import asyncpg  # type: ignore[import-not-found]

        return asyncpg
    except ModuleNotFoundError as exc:
        raise ConfigurationError(
            "AsyncpgPoolFactory requires asyncpg; install with `pip install asyncpg`."
        ) from exc


def _build_dsn(config: Mapping[str, Any], secrets: Mapping[str, str]) -> dict[str, Any]:
    """Merge non-secret driver config + resolved secrets into asyncpg kwargs.

    ``config`` carries host/port/database/user; ``secrets`` carries password
    (and optionally other auth fields). Anything in ``config`` named
    ``password`` or ``ssl_*`` is preserved verbatim, but secrets win on
    collision because the secret store is the source of truth.
    """
    merged: dict[str, Any] = {}
    for k, v in config.items():
        merged[str(k)] = v
    for k, v in secrets.items():
        merged[str(k)] = v
    return merged


class AsyncpgPool:
    def __init__(self, *, source_id: str, asyncpg_pool, acquire_timeout_s: float) -> None:
        self._source_id = source_id
        self._pool = asyncpg_pool
        self._acquire_timeout_s = acquire_timeout_s
        # Map handle id → underlying asyncpg.Connection so release can find it.
        self._inflight: dict[int, Any] = {}

    @property
    def kind(self) -> str:
        return "postgres"

    async def acquire(self, ctx: QueryContext) -> PooledConnectionHandle:
        asyncpg = _require_asyncpg()
        try:
            conn = await asyncio.wait_for(
                self._pool.acquire(), timeout=self._acquire_timeout_s
            )
        except asyncio.TimeoutError as exc:
            raise NexcraftConnectionError(
                f"Timed out waiting for a Postgres connection from pool "
                f"(source_id={self._source_id!r}, "
                f"timeout={self._acquire_timeout_s}s)"
            ) from exc
        except asyncpg.InvalidPasswordError as exc:
            raise AuthenticationError(str(exc)) from exc
        except asyncpg.PostgresError as exc:
            raise NexcraftConnectionError(str(exc)) from exc

        handle = PooledConnectionHandle(
            source_id=self._source_id, kind="postgres", raw=conn, _pool_id=self._source_id
        )
        self._inflight[id(handle)] = conn
        return handle

    async def release(self, handle: PooledConnectionHandle) -> None:
        conn = self._inflight.pop(id(handle), None)
        if conn is None:
            return
        try:
            await self._pool.release(conn)
        except Exception:
            # If release fails the connection will be discarded by asyncpg
            # via __del__; nothing useful for us to recover here.
            pass

    async def close(self) -> None:
        await self._pool.close()


class AsyncpgPoolFactory:
    """Builds an AsyncpgPool for a Postgres ``ConnectionDetails`` row."""

    async def create(
        self,
        *,
        details: ConnectionDetails,
        secrets: Mapping[str, str],
        pool_config: PoolConfig,
    ) -> DriverPool:
        if details.kind != "postgres":
            raise ConfigurationError(
                f"AsyncpgPoolFactory got non-postgres kind={details.kind!r}"
            )
        asyncpg = _require_asyncpg()

        kwargs = _build_dsn(details.config, secrets)
        # Apply per-kind extras as create_pool kwargs (e.g. statement_cache_size).
        kwargs.update(dict(pool_config.extras))

        try:
            pool = await asyncpg.create_pool(
                min_size=pool_config.min_size,
                max_size=pool_config.max_size,
                max_inactive_connection_lifetime=pool_config.idle_timeout_s or 300,
                **kwargs,
            )
        except asyncpg.InvalidPasswordError as exc:
            raise AuthenticationError(str(exc)) from exc
        except asyncpg.PostgresError as exc:
            raise NexcraftConnectionError(str(exc)) from exc

        return AsyncpgPool(
            source_id=details.source_id,
            asyncpg_pool=pool,
            acquire_timeout_s=pool_config.acquire_timeout_s,
        )


__all__ = ["AsyncpgPool", "AsyncpgPoolFactory"]
