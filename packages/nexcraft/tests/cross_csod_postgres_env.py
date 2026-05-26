"""FedSQL + asyncpg from env (same wiring as ``examples/08_postgres_env_fedsql.py``)."""

from __future__ import annotations

import os
from pathlib import Path
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


def load_dotenv_file(path: str) -> None:
    p = Path(path).expanduser()
    if not p.is_file():
        return
    for raw in p.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        os.environ.setdefault(key, value)


def ssl_kwarg() -> Any:
    mode = (os.environ.get("POSTGRES_SSL_MODE") or "").strip().lower()
    if mode in ("disable", "false", "0", "allow", "prefer"):
        return False
    if mode in ("require", "verify-ca", "verify-full", "true", "1", "on"):
        return "require"
    return True


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


class AsyncpgTableExecutor:
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


def _require_env(name: str) -> str:
    v = os.environ.get(name)
    if not v:
        raise KeyError(name)
    return v


async def build_fedsql_client(
    *,
    source_id: str = "csodworkday_pg",
    tenant_id: str = "csodworkday",
) -> tuple[FedSQLClient, PooledConnectionProvider]:
    dotenv_path = os.environ.get("NEXCRAFT_DOTENV_PATH")
    if dotenv_path:
        load_dotenv_file(dotenv_path)

    _require_env("POSTGRES_HOST")
    _require_env("POSTGRES_USER")
    _require_env("POSTGRES_DB")
    _require_env("POSTGRES_PASSWORD")

    details = ConnectionDetails(
        source_id=source_id,
        tenant_id=tenant_id,
        kind="postgres",
        display_name="CSOD Workday Postgres (hardcoded catalog)",
        config={
            "host": os.environ["POSTGRES_HOST"],
            "port": int(os.environ.get("POSTGRES_PORT", "5432")),
            "user": os.environ["POSTGRES_USER"],
            "database": os.environ["POSTGRES_DB"],
            "ssl": ssl_kwarg(),
        },
        secret_ref="env:POSTGRES_PASSWORD",
    )

    pool_min = int(os.environ.get("POSTGRES_POOL_MIN_SIZE", "1"))
    pool_max = int(os.environ.get("POSTGRES_POOL_MAX_SIZE", "5"))

    store = InMemoryManagementStore([details])
    provider = PooledConnectionProvider(
        store=store,
        factories={"postgres": AsyncpgPoolFactory()},
        pool_config=StaticPoolConfig(
            defaults={
                "postgres": PoolConfig(
                    min_size=pool_min,
                    max_size=pool_max,
                    acquire_timeout_s=30.0,
                )
            }
        ),
        secrets=EnvSecretResolver(),
    )

    catalog = InMemoryCatalog(
        {
            source_id: SourceDescriptor(
                source_id=source_id,
                kind="postgres",
                display_name=details.display_name,
                tenant_id=tenant_id,
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
    client, provider = await build_fedsql_client()
    source_id = os.environ.get("NEXCRAFT_SOURCE_ID", "csodworkday_pg")
    tenant_id = os.environ.get("NEXCRAFT_TENANT_ID", "csodworkday")
    ctx = QueryContext(tenant_id=tenant_id, query_id="cross-csod-workday-automation")
    try:
        return await client.execute_to_table(source_id, sql, ctx)
    finally:
        await provider.close()
