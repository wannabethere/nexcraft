"""Build a Postgres-backed FedSQLClient from environment variables.

This is the single-source (iteration 1) wiring for the GenieML default SQL
agent: one Postgres source, all tables resolved to it. It reuses the tested
pattern from ``packages/nexcraft/tests/cross_csod_postgres_env.py``:

  InMemoryManagementStore([ConnectionDetails]) → DBCatalog/InMemoryCatalog
  → PooledConnectionProvider(AsyncpgPoolFactory) → Router(executors={postgres})
  → FedSQLClient

Iteration 2 (cross-source) will swap the single source for a real
ManagementStore-backed catalog with a per-source table list, and assemble
multi-source results via DuckDB.

Env (see context_preparer/.env POSTGRES_* + nexcraft-jobs/.env.example):
  POSTGRES_HOST, POSTGRES_PORT, POSTGRES_DB, POSTGRES_USER, POSTGRES_PASSWORD,
  POSTGRES_SSL_MODE, POSTGRES_POOL_MIN_SIZE, POSTGRES_POOL_MAX_SIZE
  NEXCRAFT_DEFAULT_SOURCE_ID (default 'preview'), NEXCRAFT_TENANT_ID (default 'default')
"""

from __future__ import annotations

import os
from typing import Any

import pyarrow as pa

from nexcraft.catalog.inmemory import InMemoryCatalog
from nexcraft.client import FedSQLClient
from nexcraft.connection.asyncpg_pool import AsyncpgPoolFactory
from nexcraft.connection.management import (
    ConnectionDetails,
    EnvSecretResolver,
    InMemoryManagementStore,
)
from nexcraft.connection.pool_config import PoolConfig, StaticPoolConfig
from nexcraft.connection.pooled import PooledConnectionHandle, PooledConnectionProvider
from nexcraft.core.context import QueryContext
from nexcraft.core.descriptors import SourceDescriptor
from nexcraft.errors import ConfigurationError
from nexcraft.router import Router

# Postgres OID → Arrow type. Unknown OIDs fall back to large_string (asyncpg
# returns the Python value; pyarrow infers, but explicit mapping keeps schemas
# stable for empty result sets via describe()).
_PG_OID_TO_ARROW: dict[int, pa.DataType] = {
    16: pa.bool_(),
    20: pa.int64(),
    21: pa.int16(),
    23: pa.int32(),
    25: pa.large_string(),
    1043: pa.large_string(),
    700: pa.float32(),
    701: pa.float64(),
    1114: pa.timestamp("us"),
    1184: pa.timestamp("us", tz="UTC"),
    1082: pa.date32(),
    1700: pa.large_string(),
}


def _ssl_kwarg() -> Any:
    mode = (os.environ.get("POSTGRES_SSL_MODE") or "").strip().lower()
    if mode in ("disable", "false", "0", "allow", "prefer"):
        return False
    if mode in ("require", "verify-ca", "verify-full", "true", "1", "on"):
        return "require"
    return True


def _require_env(name: str) -> str:
    v = os.environ.get(name)
    if not v or not v.strip():
        raise ConfigurationError(
            f"Postgres source not configured: env var {name!r} is unset. "
            "Set POSTGRES_HOST/POSTGRES_DB/POSTGRES_USER/POSTGRES_PASSWORD."
        )
    return v.strip()


class AsyncpgTableExecutor:
    """SourceExecutor (kind='postgres') — runs SQL on an asyncpg handle → Arrow.

    Promoted from the tested cross_csod_postgres_env wiring so workers and the
    submit path can import one canonical implementation.
    """

    @property
    def kind(self) -> str:
        return "postgres"

    async def describe(self, sql: str, ctx: QueryContext, conn: Any) -> pa.Schema:
        if not isinstance(conn, PooledConnectionHandle):
            raise ConfigurationError("AsyncpgTableExecutor expects a pooled asyncpg handle")
        pg = conn.raw
        stmt = await pg.prepare(sql)
        fields: list[pa.Field] = []
        for attr in stmt.get_attributes():
            oid = attr.type.oid
            fields.append(pa.field(attr.name, _PG_OID_TO_ARROW.get(oid, pa.large_string())))
        return pa.schema(fields)

    async def execute(self, sql: str, ctx: QueryContext, conn: Any):
        if not isinstance(conn, PooledConnectionHandle):
            raise ConfigurationError("AsyncpgTableExecutor expects a pooled asyncpg handle")
        pg = conn.raw
        records = await pg.fetch(sql)
        if not records:
            schema = await self.describe(sql, ctx, conn)
            yield pa.RecordBatch.from_pylist([], schema=schema)
            return
        rows = [dict(r) for r in records]
        table = pa.Table.from_pylist(rows)
        for batch in table.to_batches():
            yield batch


def default_source_id() -> str:
    return (os.environ.get("NEXCRAFT_DEFAULT_SOURCE_ID") or "preview").strip()


def default_tenant_id() -> str:
    return (os.environ.get("NEXCRAFT_TENANT_ID") or "default").strip()


def build_postgres_fedsql_client(
    *,
    source_id: str | None = None,
    tenant_id: str | None = None,
) -> tuple[FedSQLClient, PooledConnectionProvider]:
    """FedSQLClient backed by ONE Postgres source defined from POSTGRES_* env.

    Returns ``(client, provider)`` — the caller owns ``provider.close()``.
    """
    sid = source_id or default_source_id()
    tid = tenant_id or default_tenant_id()

    host = _require_env("POSTGRES_HOST")
    user = _require_env("POSTGRES_USER")
    database = _require_env("POSTGRES_DB")
    _require_env("POSTGRES_PASSWORD")  # resolved via secret_ref below

    details = ConnectionDetails(
        source_id=sid,
        tenant_id=tid,
        kind="postgres",
        display_name=f"GenieML Postgres ({sid})",
        config={
            "host": host,
            "port": int(os.environ.get("POSTGRES_PORT", "5432")),
            "user": user,
            "database": database,
            "ssl": _ssl_kwarg(),
        },
        secret_ref="env:POSTGRES_PASSWORD",
    )

    store = InMemoryManagementStore([details])
    provider = PooledConnectionProvider(
        store=store,
        factories={"postgres": AsyncpgPoolFactory()},
        pool_config=StaticPoolConfig(
            defaults={
                "postgres": PoolConfig(
                    min_size=int(os.environ.get("POSTGRES_POOL_MIN_SIZE", "1")),
                    max_size=int(os.environ.get("POSTGRES_POOL_MAX_SIZE", "5")),
                    acquire_timeout_s=float(os.environ.get("POSTGRES_ACQUIRE_TIMEOUT_S", "30")),
                )
            }
        ),
        secrets=EnvSecretResolver(),
    )
    catalog = InMemoryCatalog(
        {
            sid: SourceDescriptor(
                source_id=sid,
                kind="postgres",
                display_name=details.display_name,
                tenant_id=tid,
                config={},
            )
        }
    )
    router = Router(
        catalog=catalog,
        connection_provider=provider,
        executors={"postgres": AsyncpgTableExecutor()},
    )
    return FedSQLClient(router), provider


async def run_sql_with_env(sql: str) -> pa.Table:
    """Convenience: run one SQL against the env-configured Postgres source.

    Useful for a smoke test independent of Temporal (``await run_sql_with_env(
    "SELECT 1 AS n")``).
    """
    client, provider = build_postgres_fedsql_client()
    ctx = QueryContext(tenant_id=default_tenant_id(), query_id="genieml-smoke")
    try:
        return await client.execute_to_table(default_source_id(), sql, ctx)
    finally:
        await provider.close()
