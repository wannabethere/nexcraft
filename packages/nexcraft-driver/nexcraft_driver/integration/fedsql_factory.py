"""Build a FedSQLClient with Postgres + Snowflake + Delta-Lake + Iceberg
sources registered from environment variables. Lets a single recipe extract
from any source through the same FedSQL surface — only the `source_id`
parameter differs.

This stays intentionally minimal:
  • Postgres: nexcraft's `PooledConnectionProvider` + asyncpg (production-grade).
  • Snowflake: a single shared snowflake.connector handle (demo-grade pooling).
  • Delta / Iceberg: one DuckDB connection per format with the relevant
    extension loaded — `LakehouseExecutor` owns it, the connection-handle
    abstraction is a no-op shell.

When FlightSQL lands you'll add a `FlightSqlExecutor` next to these without
touching the recipe code.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any

from nexcraft.catalog.inmemory import InMemoryCatalog
from nexcraft.client import FedSQLClient
from nexcraft.core.context import QueryContext
from nexcraft.core.descriptors import ConnectionHandle, SourceDescriptor
from nexcraft.errors import ConfigurationError
from nexcraft.router import Router

from nexcraft_driver.integration.source_executors import (
    AsyncpgTableExecutor,
    LakehouseExecutor,
    SnowflakeTableExecutor,
)


SUPPORTED_KINDS = ("postgres", "snowflake", "delta", "iceberg")

POSTGRES_SOURCE_ID  = "cornerstone_pg"
SNOWFLAKE_SOURCE_ID = "pricemedic_sf"
DELTA_SOURCE_ID     = "lakehouse_delta"
ICEBERG_SOURCE_ID   = "lakehouse_iceberg"

# Logical view name registered inside each LakehouseExecutor's DuckDB. The
# recipe references this as `params["table"]`.
LAKEHOUSE_VIEW_NAME = "facts"


# --- Connection handles -----------------------------------------------------

@dataclass
class _SnowflakeConnectionHandle(ConnectionHandle):
    raw: Any = None


@dataclass
class _LakehouseConnectionHandle(ConnectionHandle):
    """No-op shell: the LakehouseExecutor owns its DuckDB connection."""


class _MultiSourceConnectionProvider:
    """Dispatches by source_id: static handles for Snowflake / lakehouse
    sources, the nexcraft pooled provider for Postgres."""

    def __init__(self, pg_provider, static_handles: dict[str, ConnectionHandle]) -> None:
        self._pg = pg_provider
        self._static = dict(static_handles)

    async def acquire(self, source_id: str, ctx: QueryContext) -> ConnectionHandle:
        if source_id in self._static:
            return self._static[source_id]
        if self._pg is not None and source_id == POSTGRES_SOURCE_ID:
            return await self._pg.acquire(source_id, ctx)
        raise ConfigurationError(
            f"No connection configured for source_id={source_id!r}. "
            f"Check env vars (see .env.example)."
        )

    async def release(self, handle: ConnectionHandle) -> None:
        if handle in self._static.values():
            return None
        if self._pg is not None:
            await self._pg.release(handle)


# --- Factory ---------------------------------------------------------------

async def build_cross_source_fedsql() -> tuple[FedSQLClient, _MultiSourceConnectionProvider]:
    """Build a FedSQLClient with whichever sources have env vars set.
    At least one source must be configured."""
    sources: dict[str, SourceDescriptor] = {}
    executors: dict[str, Any] = {}
    static_handles: dict[str, ConnectionHandle] = {}
    pg_provider = None

    # Postgres
    if _has_postgres_env():
        desc, pg_provider = await _build_postgres_components()
        sources[desc.source_id] = desc
        executors["postgres"] = AsyncpgTableExecutor()

    # Snowflake
    if _has_snowflake_env():
        desc, handle = _build_snowflake_components()
        sources[desc.source_id] = desc
        static_handles[desc.source_id] = handle
        executors["snowflake"] = SnowflakeTableExecutor()

    # Delta Lake
    if _has_delta_env():
        desc, handle, executor = _build_lakehouse_components(
            source_id=DELTA_SOURCE_ID, format="delta",
            display_name="Delta Lake on S3",
            path_env="DELTA_TABLE_S3_PATH",
        )
        sources[desc.source_id] = desc
        static_handles[desc.source_id] = handle
        executors["delta"] = executor

    # Iceberg
    if _has_iceberg_env():
        desc, handle, executor = _build_lakehouse_components(
            source_id=ICEBERG_SOURCE_ID, format="iceberg",
            display_name="Iceberg on S3",
            path_env="ICEBERG_TABLE_S3_PATH",
        )
        sources[desc.source_id] = desc
        static_handles[desc.source_id] = handle
        executors["iceberg"] = executor

    if not sources:
        raise ConfigurationError(
            "No source credentials configured. Set env vars for at least one of: "
            "Postgres (POSTGRES_HOST/USER/DB/PASSWORD), "
            "Snowflake (SNOWFLAKE_ACCOUNT/USER/DATABASE/SCHEMA/WAREHOUSE/PASSWORD), "
            "Delta (DELTA_TABLE_S3_PATH + AWS_*), "
            "Iceberg (ICEBERG_TABLE_S3_PATH + AWS_*). See .env.example."
        )

    provider = _MultiSourceConnectionProvider(pg_provider=pg_provider,
                                              static_handles=static_handles)
    router = Router(
        catalog=InMemoryCatalog(sources),
        connection_provider=provider,
        executors=executors,
    )
    return FedSQLClient(router), provider


# --- env checks -------------------------------------------------------------

def _has_postgres_env() -> bool:
    return all(os.environ.get(k) for k in
               ("POSTGRES_HOST", "POSTGRES_USER", "POSTGRES_DB", "POSTGRES_PASSWORD"))


def _has_snowflake_env() -> bool:
    return all(os.environ.get(k) for k in
               ("SNOWFLAKE_ACCOUNT", "SNOWFLAKE_USER", "SNOWFLAKE_DATABASE",
                "SNOWFLAKE_SCHEMA", "SNOWFLAKE_WAREHOUSE", "SNOWFLAKE_PASSWORD"))


def _has_delta_env() -> bool:
    return bool(os.environ.get("DELTA_TABLE_S3_PATH"))


def _has_iceberg_env() -> bool:
    return bool(os.environ.get("ICEBERG_TABLE_S3_PATH"))


# --- component builders -----------------------------------------------------

async def _build_postgres_components():
    from nexcraft.connection.asyncpg_pool import AsyncpgPoolFactory
    from nexcraft.connection.management import (
        ConnectionDetails, EnvSecretResolver, InMemoryManagementStore,
    )
    from nexcraft.connection.pool_config import PoolConfig, StaticPoolConfig
    from nexcraft.connection.pooled import PooledConnectionProvider

    details = ConnectionDetails(
        source_id=POSTGRES_SOURCE_ID,
        tenant_id="default",
        kind="postgres",
        display_name="Cornerstone Postgres",
        config={
            "host": os.environ["POSTGRES_HOST"],
            "port": int(os.environ.get("POSTGRES_PORT", "5432")),
            "user": os.environ["POSTGRES_USER"],
            "database": os.environ["POSTGRES_DB"],
            "ssl": os.environ.get("POSTGRES_SSL_MODE", "require"),
        },
        secret_ref="env:POSTGRES_PASSWORD",
    )
    provider = PooledConnectionProvider(
        store=InMemoryManagementStore([details]),
        factories={"postgres": AsyncpgPoolFactory()},
        pool_config=StaticPoolConfig(
            defaults={"postgres": PoolConfig(
                min_size=int(os.environ.get("POSTGRES_POOL_MIN_SIZE", "1")),
                max_size=int(os.environ.get("POSTGRES_POOL_MAX_SIZE", "5")),
                acquire_timeout_s=30.0,
            )},
        ),
        secrets=EnvSecretResolver(),
    )
    descriptor = SourceDescriptor(
        source_id=POSTGRES_SOURCE_ID, kind="postgres",
        display_name=details.display_name, tenant_id="default", config={},
    )
    return descriptor, provider


def _build_snowflake_components():
    import snowflake.connector  # type: ignore[import-not-found]
    con = snowflake.connector.connect(
        account=os.environ["SNOWFLAKE_ACCOUNT"],
        user=os.environ["SNOWFLAKE_USER"],
        password=os.environ["SNOWFLAKE_PASSWORD"],
        database=os.environ["SNOWFLAKE_DATABASE"],
        schema=os.environ["SNOWFLAKE_SCHEMA"],
        warehouse=os.environ["SNOWFLAKE_WAREHOUSE"],
        role=os.environ.get("SNOWFLAKE_ROLE") or None,
    )
    handle = _SnowflakeConnectionHandle(source_id=SNOWFLAKE_SOURCE_ID, kind="snowflake", raw=con)
    descriptor = SourceDescriptor(
        source_id=SNOWFLAKE_SOURCE_ID, kind="snowflake",
        display_name="PriceMedic Snowflake", tenant_id="default", config={},
    )
    return descriptor, handle


def _build_lakehouse_components(
    *, source_id: str, format: str, display_name: str, path_env: str,
):
    """Construct a SourceDescriptor + (shell) ConnectionHandle + LakehouseExecutor
    for a Delta or Iceberg table on S3. Reads the table's S3 path from the
    given env var and uses LAKEHOUSE_VIEW_NAME as the logical view name."""
    path = os.environ[path_env]
    region = os.environ.get("AWS_REGION") or os.environ.get("AWS_DEFAULT_REGION")
    executor = LakehouseExecutor(
        format=format,
        tables={LAKEHOUSE_VIEW_NAME: path},
        region=region,
    )
    handle = _LakehouseConnectionHandle(source_id=source_id, kind=format)
    descriptor = SourceDescriptor(
        source_id=source_id, kind=format,
        display_name=display_name, tenant_id="default", config={"s3_path": path},
    )
    return descriptor, handle, executor
