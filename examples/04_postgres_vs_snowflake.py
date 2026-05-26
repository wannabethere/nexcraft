"""
Example: cross-source recipe — Postgres ``users`` joined with Snowflake ``orders``.

Highlights how nexcraft's single-source-per-query rule plays with dialect-specific
SQL: each source gets its native SQL pushed down (Postgres dialect for the
Postgres executor, Snowflake dialect for the Snowflake executor), and the
recipe joins the two streams in DuckDB.

This script uses two ``MemoryExecutor`` instances stubbed in as ``postgres`` and
``snowflake`` so it runs locally with no infrastructure. In production you'd
swap in real ADBC/driver-backed executors with the same ``kind`` strings; the
recipe code does not change.

Usage (from repo root):
    python examples/04_postgres_vs_snowflake.py
"""

from __future__ import annotations

import asyncio
from typing import Any, Mapping

import pyarrow as pa

from nexcraft.catalog.inmemory import InMemoryCatalog
from nexcraft.client import FedSQLClient
from nexcraft.connection.static import StaticConnectionProvider
from nexcraft.core.context import QueryContext
from nexcraft.core.descriptors import ConnectionHandle, SourceDescriptor
from nexcraft.executors.memory import MemoryExecutor
from nexcraft.router import Router

from nexcraft_jobs.context import JobContext
from nexcraft_jobs.runtime.local import LocalRuntime
from nexcraft_jobs.types import ComputeResult


# ---------------------------------------------------------------------------
# Dialect-specific SQL.
#
# Postgres uses double-quoted identifiers, EXTRACT(EPOCH FROM ts), and
# TO_CHAR for date formatting. Snowflake uses DATE_PART('epoch_second', ts),
# DATE_TRUNC('MONTH', ts), and IFF for inline conditionals. Both run under
# the same FedSQLClient; the executor for that source.kind sees its native
# dialect and pushes the query down without translation.
# ---------------------------------------------------------------------------
POSTGRES_USERS_SQL = """
SELECT
    "id"          AS user_id,
    "email"       AS email,
    "signup_date" AS signup_date,
    EXTRACT(EPOCH FROM "signup_date")::BIGINT AS signup_epoch
FROM "users"
WHERE "signup_date" >= DATE '2026-01-01'
ORDER BY "id"
""".strip()

SNOWFLAKE_ORDERS_SQL = """
SELECT
    USER_ID                                  AS user_id,
    TOTAL                                    AS total,
    DATE_TRUNC('MONTH', ORDER_TS)            AS order_month,
    DATE_PART('epoch_second', ORDER_TS)      AS order_epoch,
    IFF(TOTAL >= 1000, 'high', 'normal')     AS tier
FROM ORDERS
WHERE ORDER_TS >= '2026-01-01'
QUALIFY ROW_NUMBER() OVER (PARTITION BY USER_ID, DATE_TRUNC('MONTH', ORDER_TS)
                           ORDER BY ORDER_TS DESC) = 1
""".strip()


# ---------------------------------------------------------------------------
# Demo data: pretend Postgres + Snowflake.
# ---------------------------------------------------------------------------
def _users_batch() -> list[pa.RecordBatch]:
    return [
        pa.RecordBatch.from_arrays(
            [
                pa.array([1, 2, 3], type=pa.int64()),
                pa.array(["alice@a.io", "bob@b.io", "carol@c.io"]),
                pa.array(["2026-02-01", "2026-03-15", "2026-04-02"]),
                pa.array(
                    [1_769_904_000, 1_773_532_800, 1_775_433_600], type=pa.int64()
                ),
            ],
            names=["user_id", "email", "signup_date", "signup_epoch"],
        )
    ]


def _orders_batch() -> list[pa.RecordBatch]:
    return [
        pa.RecordBatch.from_arrays(
            [
                pa.array([1, 1, 2, 3, 3], type=pa.int64()),
                pa.array([250.0, 1500.0, 75.0, 3000.0, 200.0]),
                pa.array(
                    ["2026-02-01", "2026-03-01", "2026-03-01", "2026-04-01", "2026-05-01"]
                ),
                pa.array(
                    [
                        1_769_904_000,
                        1_772_582_400,
                        1_772_582_400,
                        1_775_347_200,
                        1_777_939_200,
                    ],
                    type=pa.int64(),
                ),
                pa.array(["normal", "high", "normal", "high", "normal"]),
            ],
            names=["user_id", "total", "order_month", "order_epoch", "tier"],
        )
    ]


def build_cross_source_client() -> FedSQLClient:
    """Wire two sources of different kinds behind one FedSQLClient."""
    pg_executor = MemoryExecutor(replies={POSTGRES_USERS_SQL: _users_batch()})
    sf_executor = MemoryExecutor(replies={SNOWFLAKE_ORDERS_SQL: _orders_batch()})

    catalog = InMemoryCatalog(
        {
            "prod_pg": SourceDescriptor(
                source_id="prod_pg",
                kind="postgres",
                display_name="Production Postgres",
                tenant_id="default",
                config={"host": "pg.internal", "database": "prod"},
            ),
            "warehouse": SourceDescriptor(
                source_id="warehouse",
                kind="snowflake",
                display_name="Snowflake Warehouse",
                tenant_id="default",
                config={"account": "acme", "warehouse": "ANALYTICS_WH"},
            ),
        }
    )

    # MemoryExecutor uses kind="memory"; for the demo we wrap each instance
    # so its kind matches the descriptor it's serving.
    class _Reskinned:
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

    provider = StaticConnectionProvider(
        {
            "prod_pg": ConnectionHandle(source_id="prod_pg", kind="postgres"),
            "warehouse": ConnectionHandle(source_id="warehouse", kind="snowflake"),
        }
    )

    router = Router(
        catalog=catalog,
        connection_provider=provider,
        executors={
            "postgres": _Reskinned(pg_executor, "postgres"),
            "snowflake": _Reskinned(sf_executor, "snowflake"),
        },
    )
    return FedSQLClient(router)


# ---------------------------------------------------------------------------
# Recipe: pulls from both sources independently (single-source-per-query),
# joins in DuckDB. This is the canonical pattern from ADR 004.
# ---------------------------------------------------------------------------
class PostgresSnowflakeLifetimeValueRecipe:
    name = "lifetime_value_pg_snowflake"
    version = "1.0.0"

    def validate(self, params: Mapping[str, Any]) -> None:
        for key in ("postgres_source", "snowflake_source"):
            if key not in params:
                raise ValueError(f"missing param: {key!r}")

    async def extract(self, params, ctx, fedsql):
        users_qc = ctx.derive_query_context(f"{ctx.job_id}-users")
        orders_qc = ctx.derive_query_context(f"{ctx.job_id}-orders")
        return {
            "users": await fedsql.execute_to_reader(
                params["postgres_source"], POSTGRES_USERS_SQL, users_qc
            ),
            "orders": await fedsql.execute_to_reader(
                params["snowflake_source"], SNOWFLAKE_ORDERS_SQL, orders_qc
            ),
        }

    async def compute(self, inputs, params, ctx):
        con = ctx._duckdb
        primary = con.execute(
            """
            SELECT
                u.user_id,
                u.email,
                u.signup_date,
                COUNT(o.user_id)        AS order_count,
                COALESCE(SUM(o.total), 0) AS lifetime_value,
                COUNT_IF(o.tier = 'high') AS high_tier_orders
            FROM users u
            LEFT JOIN orders o USING (user_id)
            GROUP BY u.user_id, u.email, u.signup_date
            ORDER BY lifetime_value DESC
            """
        ).to_arrow_table()

        by_month = con.execute(
            """
            SELECT order_month, SUM(total) AS total_revenue, COUNT(*) AS orders
            FROM orders
            GROUP BY order_month
            ORDER BY order_month
            """
        ).to_arrow_table()

        return ComputeResult(
            primary=primary,
            auxiliaries={"by_month": by_month},
            metadata={
                "users_seen": int(primary.num_rows),
                "high_tier_total": int(
                    sum(primary.column("high_tier_orders").to_pylist())
                ),
            },
        )

    async def persist(self, result, params, ctx, store):
        return await store.finalize(ctx, result, params)


async def main() -> None:
    client = build_cross_source_client()
    runtime = LocalRuntime(client)
    ref = await runtime.submit(
        PostgresSnowflakeLifetimeValueRecipe(),
        params={"postgres_source": "prod_pg", "snowflake_source": "warehouse"},
        ctx=JobContext(tenant_id="default", job_id="ltv-demo-1"),
    )
    print("ResultRef:", ref)

    # Re-run extract to print the underlying tables for clarity.
    pg_users = await client.execute_to_table(
        "prod_pg",
        POSTGRES_USERS_SQL,
        QueryContext(tenant_id="default", query_id="print-users"),
    )
    sf_orders = await client.execute_to_table(
        "warehouse",
        SNOWFLAKE_ORDERS_SQL,
        QueryContext(tenant_id="default", query_id="print-orders"),
    )
    print("\n-- Postgres users (Postgres dialect SQL) --")
    print(pg_users.to_pydict())
    print("\n-- Snowflake orders (Snowflake dialect SQL) --")
    print(sf_orders.to_pydict())


if __name__ == "__main__":
    asyncio.run(main())
