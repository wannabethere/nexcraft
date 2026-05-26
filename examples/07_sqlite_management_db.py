"""
Example: dummy SQLite management DB → DBCatalog + PooledConnectionProvider →
                                        execute Postgres-dialect and
                                        Snowflake-dialect queries.

What this shows end-to-end:

  1. A toy ``connection_details`` table in SQLite (stand-in for the host
     system's connections store / GenieML ConnectionDetails).
  2. A ``SqliteManagementStore`` that implements ``ManagementStore`` against
     that table.
  3. Wiring:  DBCatalog(store) + PooledConnectionProvider(store, ...) +
              Router(catalog, provider, executors) + FedSQLClient(router).
  4. A Postgres-dialect query and a Snowflake-dialect query running through
     the same client; each goes to its source's executor.
  5. A tenant-boundary check: another tenant's source_id is rejected with
     ``AuthenticationError`` before any driver is touched.

Real Postgres/Snowflake drivers are not used (the example is dependency-free):
the executors are ``MemoryExecutor`` instances re-skinned to advertise
``kind='postgres'`` / ``kind='snowflake'``. Swap in real driver-backed
executors + real ``DriverPoolFactory`` (e.g. ``AsyncpgPoolFactory``) and the
calling code does not change.

Usage (from repo root):
    python examples/07_sqlite_management_db.py
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
import tempfile
from pathlib import Path
from typing import Any, Mapping

import pyarrow as pa

from nexcraft.catalog.db import DBCatalog
from nexcraft.client import FedSQLClient
from nexcraft.connection.management import ConnectionDetails
from nexcraft.connection.pool_config import PoolConfig, YamlPoolConfig
from nexcraft.connection.pooled import (
    DriverPool,
    DriverPoolFactory,
    PooledConnectionHandle,
    PooledConnectionProvider,
)
from nexcraft.core.context import QueryContext
from nexcraft.errors import AuthenticationError, ConfigurationError
from nexcraft.executors.memory import MemoryExecutor
from nexcraft.router import Router


# ===========================================================================
# 1. The dummy management DB.
# ===========================================================================
SCHEMA = """
CREATE TABLE IF NOT EXISTS connection_details (
    source_id     TEXT PRIMARY KEY,
    tenant_id     TEXT NOT NULL,
    kind          TEXT NOT NULL,
    display_name  TEXT NOT NULL,
    config_json   TEXT NOT NULL,
    secret_ref    TEXT,
    tags_json     TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_connection_details_tenant
    ON connection_details (tenant_id);
"""

DUMMY_ROWS: list[tuple[str, str, str, str, dict, str | None, dict]] = [
    (
        "prod_pg",
        "acme",
        "postgres",
        "ACME Production Postgres",
        {"host": "pg.acme.internal", "database": "prod", "user": "app"},
        "env:PG_PROD_PASSWORD",
        {"team": "data", "env": "prod"},
    ),
    (
        "warehouse",
        "acme",
        "snowflake",
        "ACME Snowflake Warehouse",
        {"account": "acme", "warehouse": "ANALYTICS_WH", "user": "etl"},
        "env:SF_PASSWORD",
        {"team": "analytics"},
    ),
    (
        "other_tenant_pg",
        "other-corp",
        "postgres",
        "Other Corp Postgres",
        {"host": "pg.other.internal", "database": "prod"},
        None,
        {},
    ),
]


def init_dummy_db(path: Path) -> None:
    con = sqlite3.connect(path)
    try:
        con.executescript(SCHEMA)
        con.executemany(
            "INSERT OR REPLACE INTO connection_details "
            "(source_id, tenant_id, kind, display_name, config_json, secret_ref, tags_json) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            [
                (
                    sid,
                    tid,
                    kind,
                    name,
                    json.dumps(cfg),
                    sref,
                    json.dumps(tags),
                )
                for (sid, tid, kind, name, cfg, sref, tags) in DUMMY_ROWS
            ],
        )
        con.commit()
    finally:
        con.close()


def dump_db_table(path: Path) -> None:
    con = sqlite3.connect(path)
    try:
        rows = con.execute(
            "SELECT source_id, tenant_id, kind, display_name, secret_ref FROM connection_details"
        ).fetchall()
    finally:
        con.close()
    print("  source_id            tenant_id    kind       display_name                   secret_ref")
    for r in rows:
        print(f"  {r[0]:<20} {r[1]:<12} {r[2]:<10} {r[3]:<30} {r[4] or '-'}")


# ===========================================================================
# 2. SqliteManagementStore — adapter from the connection_details table to the
#    nexcraft.ManagementStore protocol. Run blocking sqlite3 calls inside
#    asyncio.to_thread so we don't stall the event loop.
# ===========================================================================
def _row_to_details(row: tuple) -> ConnectionDetails:
    return ConnectionDetails(
        source_id=row[0],
        tenant_id=row[1],
        kind=row[2],
        display_name=row[3],
        config=json.loads(row[4]),
        secret_ref=row[5],
        tags=json.loads(row[6]),
    )


class SqliteManagementStore:
    """Looks up ConnectionDetails by source_id, list by tenant_id.

    Uses a fresh sqlite3 connection per call; production code would hold a
    pool or a SQLAlchemy session — the protocol doesn't dictate.
    """

    SELECT_COLS = "source_id, tenant_id, kind, display_name, config_json, secret_ref, tags_json"

    def __init__(self, path: Path) -> None:
        self._path = path

    async def get_connection_details(self, source_id: str) -> ConnectionDetails:
        def _go() -> tuple | None:
            con = sqlite3.connect(self._path)
            try:
                return con.execute(
                    f"SELECT {self.SELECT_COLS} "
                    "FROM connection_details WHERE source_id = ?",
                    (source_id,),
                ).fetchone()
            finally:
                con.close()

        row = await asyncio.to_thread(_go)
        if row is None:
            raise ConfigurationError(f"Unknown source_id={source_id!r}")
        return _row_to_details(row)

    async def list_connection_details(
        self, tenant_id: str | None = None
    ) -> list[ConnectionDetails]:
        def _go() -> list[tuple]:
            con = sqlite3.connect(self._path)
            try:
                if tenant_id is None:
                    return con.execute(
                        f"SELECT {self.SELECT_COLS} FROM connection_details"
                    ).fetchall()
                return con.execute(
                    f"SELECT {self.SELECT_COLS} "
                    "FROM connection_details WHERE tenant_id = ?",
                    (tenant_id,),
                ).fetchall()
            finally:
                con.close()

        rows = await asyncio.to_thread(_go)
        return [_row_to_details(r) for r in rows]


# ===========================================================================
# 3. Stub DriverPool / DriverPoolFactory — production replaces these with
#    AsyncpgPoolFactory + a Snowflake equivalent. The shape stays identical.
# ===========================================================================
class _DemoPool:
    def __init__(self, *, source_id: str, kind: str, sizing: PoolConfig) -> None:
        self._source_id = source_id
        self._kind = kind
        self._sizing = sizing
        self._open = False
        self.checkouts = 0

    @property
    def kind(self) -> str:
        return self._kind

    async def acquire(self, ctx: QueryContext) -> PooledConnectionHandle:
        if not self._open:
            print(
                f"  [pool {self._source_id}] open "
                f"(min={self._sizing.min_size}, max={self._sizing.max_size})"
            )
            self._open = True
        self.checkouts += 1
        return PooledConnectionHandle(
            source_id=self._source_id,
            kind=self._kind,
            raw=f"<fake-{self._kind}-conn>",
            _pool_id=self._source_id,
        )

    async def release(self, handle: PooledConnectionHandle) -> None:
        return None

    async def close(self) -> None:
        print(f"  [pool {self._source_id}] close")


class _DemoFactory:
    def __init__(self, kind: str) -> None:
        self._kind = kind

    async def create(
        self,
        *,
        details: ConnectionDetails,
        secrets: Mapping[str, str],
        pool_config: PoolConfig,
    ) -> DriverPool:
        print(
            f"  [factory {self._kind}] build pool for {details.source_id!r} "
            f"using config={dict(details.config)} secrets-keys={list(secrets.keys())}"
        )
        return _DemoPool(
            source_id=details.source_id, kind=self._kind, sizing=pool_config
        )


# ===========================================================================
# 4. Dialect-specific SQL + canned responses for the stub executors.
# ===========================================================================
POSTGRES_RECENT_USERS_SQL = """
SELECT
    "id"          AS user_id,
    "email"       AS email,
    "signup_date" AS signup_date,
    EXTRACT(EPOCH FROM "signup_date")::BIGINT AS signup_epoch
FROM "users"
WHERE "signup_date" >= DATE '2026-04-01'
ORDER BY "id"
""".strip()

SNOWFLAKE_RECENT_ORDERS_SQL = """
SELECT
    USER_ID                              AS user_id,
    TOTAL                                AS total,
    DATE_TRUNC('MONTH', ORDER_TS)        AS order_month,
    IFF(TOTAL >= 1000, 'high', 'normal') AS tier
FROM ORDERS
WHERE ORDER_TS >= '2026-04-01'
QUALIFY ROW_NUMBER() OVER (
    PARTITION BY USER_ID ORDER BY ORDER_TS DESC
) = 1
""".strip()


def _users_batch() -> list[pa.RecordBatch]:
    return [
        pa.RecordBatch.from_arrays(
            [
                pa.array([3, 4, 5], type=pa.int64()),
                pa.array(["carol@c.io", "dave@d.io", "erin@e.io"]),
                pa.array(["2026-04-02", "2026-04-12", "2026-05-01"]),
                pa.array(
                    [1_775_433_600, 1_776_297_600, 1_777_939_200], type=pa.int64()
                ),
            ],
            names=["user_id", "email", "signup_date", "signup_epoch"],
        )
    ]


def _orders_batch() -> list[pa.RecordBatch]:
    return [
        pa.RecordBatch.from_arrays(
            [
                pa.array([3, 4, 5], type=pa.int64()),
                pa.array([3000.0, 200.0, 850.0]),
                pa.array(["2026-04-01", "2026-05-01", "2026-05-01"]),
                pa.array(["high", "normal", "normal"]),
            ],
            names=["user_id", "total", "order_month", "tier"],
        )
    ]


class _Reskinned:
    """Wraps a MemoryExecutor so its advertised kind matches the source."""

    def __init__(self, inner: MemoryExecutor, kind: str) -> None:
        self._inner = inner
        self._kind = kind

    @property
    def kind(self) -> str:
        return self._kind

    async def describe(self, sql, ctx, conn):
        return await self._inner.describe(sql, ctx, conn)

    def execute(self, sql, ctx, conn):
        return self._inner.execute(sql, ctx, conn)


# ===========================================================================
# Per-kind pool config — operator-tunable, NOT in the management DB.
# ===========================================================================
POOL_CONFIG_YAML = """
defaults:
  postgres:
    min_size: 2
    max_size: 20
    acquire_timeout_s: 5
    statement_cache_size: 1024
  snowflake:
    min_size: 1
    max_size: 8
    acquire_timeout_s: 10
overrides:
  prod_pg:
    min_size: 5
    max_size: 50
"""


# ===========================================================================
# 5. Glue everything together and run two queries.
# ===========================================================================
async def main() -> None:
    import os

    os.environ.setdefault("PG_PROD_PASSWORD", "stub-pg-pw")
    os.environ.setdefault("SF_PASSWORD", "stub-sf-pw")

    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = Path(tmpdir) / "management.db"
        init_dummy_db(db_path)

        print("\n[1] connection_details table seeded:")
        dump_db_table(db_path)

        store = SqliteManagementStore(db_path)
        catalog = DBCatalog(store)
        from nexcraft.connection.management import EnvSecretResolver

        provider = PooledConnectionProvider(
            store=store,
            factories={
                "postgres": _DemoFactory("postgres"),
                "snowflake": _DemoFactory("snowflake"),
            },
            pool_config=YamlPoolConfig.from_string(POOL_CONFIG_YAML),
            secrets=EnvSecretResolver(),
        )

        # The executors are stubbed but they advertise the right `kind` so the
        # router accepts them. Each gets its own canned reply for the dialect-
        # specific SQL we send.
        pg_executor = _Reskinned(
            MemoryExecutor(replies={POSTGRES_RECENT_USERS_SQL: _users_batch()}),
            "postgres",
        )
        sf_executor = _Reskinned(
            MemoryExecutor(replies={SNOWFLAKE_RECENT_ORDERS_SQL: _orders_batch()}),
            "snowflake",
        )

        router = Router(
            catalog=catalog,
            connection_provider=provider,
            executors={"postgres": pg_executor, "snowflake": sf_executor},
        )
        client = FedSQLClient(router)

        # ---------------------------------------------------------------
        print("\n[2] DBCatalog.list_sources(tenant='acme')")
        for src in await catalog.list_sources(tenant_id="acme"):
            print(f"  - {src.source_id} (kind={src.kind}, name={src.display_name})")

        # ---------------------------------------------------------------
        ctx_acme = QueryContext(tenant_id="acme", query_id="q-pg-1")
        print("\n[3] Postgres query (Postgres-dialect SQL through prod_pg):")
        pg_users = await client.execute_to_table(
            "prod_pg", POSTGRES_RECENT_USERS_SQL, ctx_acme
        )
        print(f"  rows={pg_users.num_rows}")
        print(f"  data={pg_users.to_pydict()}")

        # ---------------------------------------------------------------
        ctx_acme = QueryContext(tenant_id="acme", query_id="q-sf-1")
        print("\n[4] Snowflake query (Snowflake-dialect SQL through warehouse):")
        sf_orders = await client.execute_to_table(
            "warehouse", SNOWFLAKE_RECENT_ORDERS_SQL, ctx_acme
        )
        print(f"  rows={sf_orders.num_rows}")
        print(f"  data={sf_orders.to_pydict()}")

        # ---------------------------------------------------------------
        print("\n[5] Cross-tenant attempt: tenant 'acme' tries 'other_tenant_pg'")
        ctx_bad = QueryContext(tenant_id="acme", query_id="q-bad-1")
        try:
            await client.execute_to_table(
                "other_tenant_pg",
                "SELECT 1",  # never reached — provider rejects first
                ctx_bad,
            )
        except AuthenticationError as exc:
            print(f"  raised AuthenticationError as expected: {exc}")

        # ---------------------------------------------------------------
        print("\n[6] Re-running the Postgres query — pool is reused:")
        ctx_acme = QueryContext(tenant_id="acme", query_id="q-pg-2")
        await client.execute_to_table("prod_pg", POSTGRES_RECENT_USERS_SQL, ctx_acme)

        await provider.close()


if __name__ == "__main__":
    asyncio.run(main())
