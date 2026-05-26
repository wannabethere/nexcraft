"""SourceExecutor implementations for Postgres (asyncpg) and Snowflake
(snowflake-connector-python wrapped via asyncio.to_thread).

Both conform to `nexcraft.core.protocols.SourceExecutor`:
  - `kind: str`
  - `async describe(sql, ctx, conn) -> pa.Schema`
  - `execute(sql, ctx, conn) -> AsyncIterator[pa.RecordBatch]`

These exist so a recipe can extract from either source through the same
FedSQLClient surface. They're deliberately small — the goal is to demonstrate
API portability, not to replace future FlightSQL executors.
"""
from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from typing import Any

import pyarrow as pa

from nexcraft.core.context import QueryContext
from nexcraft.errors import ConfigurationError


# --- Postgres (asyncpg, native async) ---------------------------------------

# Minimal Postgres OID → Arrow mapping. Sufficient for the cornerstone tables
# (text, int, float, timestamp, date). Unknown OIDs degrade to large_string,
# matching the pattern in nexcraft's own test skeleton.
_PG_OID_TO_ARROW: dict[int, pa.DataType] = {
    16:  pa.bool_(),         # bool
    20:  pa.int64(),         # int8
    21:  pa.int16(),         # int2
    23:  pa.int32(),         # int4
    25:  pa.large_string(),  # text
    700: pa.float32(),       # float4
    701: pa.float64(),       # float8
    1043: pa.large_string(), # varchar
    1082: pa.date32(),       # date
    1114: pa.timestamp("us"),
    1184: pa.timestamp("us", tz="UTC"),
    1700: pa.large_string(), # numeric (kept as string to avoid precision loss)
}


class AsyncpgTableExecutor:
    """SourceExecutor for Postgres backed by asyncpg connections from a pool.

    The `conn` argument is a `PooledConnectionHandle` whose `.raw` is the
    asyncpg Connection. Streams the entire result as Arrow batches.
    """

    @property
    def kind(self) -> str:
        return "postgres"

    async def describe(self, sql: str, ctx: QueryContext, conn: Any) -> pa.Schema:
        pg = self._raw(conn)
        stmt = await pg.prepare(sql)
        return pa.schema([
            pa.field(a.name, _PG_OID_TO_ARROW.get(a.type.oid, pa.large_string()))
            for a in stmt.get_attributes()
        ])

    async def execute(
        self, sql: str, ctx: QueryContext, conn: Any
    ) -> AsyncIterator[pa.RecordBatch]:
        pg = self._raw(conn)
        records = await pg.fetch(sql)
        if not records:
            schema = await self.describe(sql, ctx, conn)
            yield pa.RecordBatch.from_pylist([], schema=schema)
            return
        # asyncpg rows are mapping-like; pa.Table.from_pylist infers schema.
        rows = [dict(r) for r in records]
        for batch in pa.Table.from_pylist(rows).to_batches():
            yield batch

    @staticmethod
    def _raw(conn: Any):
        # nexcraft's PooledConnectionHandle has a .raw attribute holding the
        # underlying driver connection. Import locally to keep this module
        # importable when nexcraft.connection is absent.
        from nexcraft.connection.pooled import PooledConnectionHandle
        if not isinstance(conn, PooledConnectionHandle):
            raise ConfigurationError(
                "AsyncpgTableExecutor expects a PooledConnectionHandle whose "
                "`.raw` is an asyncpg.Connection"
            )
        return conn.raw


# --- Snowflake (sync driver wrapped in asyncio.to_thread) -------------------

class SnowflakeTableExecutor:
    """SourceExecutor for Snowflake. snowflake-connector-python is sync, so
    we run each query inside `asyncio.to_thread`. Yields a single Arrow batch
    per query (the connector returns a pyarrow Table in one shot via
    `fetch_arrow_all`)."""

    @property
    def kind(self) -> str:
        return "snowflake"

    async def describe(self, sql: str, ctx: QueryContext, conn: Any) -> pa.Schema:
        # Cheapest reliable shape: prepend LIMIT 0 and let the connector
        # report the result schema via fetch_arrow_all.
        probe_sql = f"SELECT * FROM ({sql.rstrip(';')}) LIMIT 0"
        tbl = await asyncio.to_thread(self._run_to_arrow, conn, probe_sql)
        return tbl.schema

    async def execute(
        self, sql: str, ctx: QueryContext, conn: Any
    ) -> AsyncIterator[pa.RecordBatch]:
        tbl = await asyncio.to_thread(self._run_to_arrow, self._raw(conn), sql)
        for batch in tbl.to_batches():
            yield batch

    @staticmethod
    def _run_to_arrow(con: Any, sql: str) -> pa.Table:
        cur = con.cursor()
        try:
            cur.execute(sql)
            return cur.fetch_arrow_all() or pa.table({})
        finally:
            cur.close()

    @staticmethod
    def _raw(conn: Any):
        # For Snowflake we don't have a nexcraft pooled connection type yet —
        # the factory passes the snowflake.connector.SnowflakeConnection
        # directly. ConfigurationError if someone else is passed.
        if hasattr(conn, "raw"):
            return conn.raw
        if hasattr(conn, "cursor"):
            return conn
        raise ConfigurationError(
            "SnowflakeTableExecutor expects a snowflake.connector connection "
            "or a wrapper exposing it via `.raw`"
        )


# --- Lakehouse: Delta + Iceberg via DuckDB extensions ----------------------

class LakehouseExecutor:
    """SourceExecutor for Delta-Lake or Iceberg tables on S3.

    Holds a single in-memory DuckDB connection. On first use it installs and
    loads `httpfs`, `aws`, and either `delta` or `iceberg`, creates an S3
    secret via the AWS credential chain (so env vars / profile / IAM role
    all work), and pre-registers each configured table as a VIEW pointing
    at the format-specific scan function. Queries then run against the
    views like any DuckDB table — dstools templates unchanged.

    `tables`: {logical_name: s3_path}. Logical names are what the recipe's
    `params["table"]` references. The executor ignores the `conn` argument
    passed by the router (it owns its own DuckDB connection — same pattern
    as `MemoryExecutor`).
    """

    SUPPORTED_FORMATS = ("delta", "iceberg")

    def __init__(self, *, format: str, tables: dict[str, str],
                 region: str | None = None) -> None:
        if format not in self.SUPPORTED_FORMATS:
            raise ValueError(
                f"format must be one of {self.SUPPORTED_FORMATS}, got {format!r}"
            )
        self._format = format
        self._tables = dict(tables)
        self._region = region
        self._con = None  # lazy

    @property
    def kind(self) -> str:
        # Distinct kinds let the Router pick the right executor cleanly.
        return self._format

    async def describe(self, sql: str, ctx: QueryContext, conn: Any) -> pa.Schema:
        con = self._ensure_con()
        probe = f"SELECT * FROM ({sql.rstrip(';')}) AS _probe LIMIT 0"
        return await asyncio.to_thread(self._sync_schema, con, probe)

    async def execute(
        self, sql: str, ctx: QueryContext, conn: Any
    ) -> AsyncIterator[pa.RecordBatch]:
        con = self._ensure_con()
        tbl = await asyncio.to_thread(self._sync_to_arrow, con, sql)
        for batch in tbl.to_batches():
            yield batch

    def _ensure_con(self):
        if self._con is not None:
            return self._con
        import duckdb  # local import: the optional dep
        self._con = duckdb.connect(":memory:")
        self._con.execute("INSTALL httpfs; LOAD httpfs;")
        self._con.execute("INSTALL aws; LOAD aws;")
        self._con.execute(f"INSTALL {self._format}; LOAD {self._format};")
        # Standard AWS credential chain: env vars, ~/.aws/credentials, or
        # IAM instance/task role — all without code changes.
        region_clause = f", REGION '{self._region}'" if self._region else ""
        self._con.execute(
            "CREATE OR REPLACE SECRET aws_creds ("
            "TYPE S3, PROVIDER CREDENTIAL_CHAIN" + region_clause + ")"
        )
        scan_fn = "delta_scan" if self._format == "delta" else "iceberg_scan"
        for logical, path in self._tables.items():
            if not logical.replace("_", "").isalnum():
                raise ValueError(f"unsafe view name: {logical!r}")
            if not path.startswith("s3://"):
                raise ValueError(f"expected s3:// path for {logical!r}, got {path!r}")
            self._con.execute(
                f"CREATE OR REPLACE VIEW {logical} AS "
                f"SELECT * FROM {scan_fn}(?)",
                [path],
            )
        return self._con

    @staticmethod
    def _sync_schema(con, sql: str) -> pa.Schema:
        return con.execute(sql).fetch_arrow_table().schema

    @staticmethod
    def _sync_to_arrow(con, sql: str) -> pa.Table:
        return con.execute(sql).fetch_arrow_table()
