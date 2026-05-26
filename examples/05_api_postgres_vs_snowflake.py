"""
Example: direct ``FedSQLClient`` API usage against Postgres-flavoured and
Snowflake-flavoured sources.

Where ``04_postgres_vs_snowflake.py`` shows the recipe pattern (extract →
DuckDB → persist), this example sticks to the federation primitive: ``describe``,
``execute`` (streaming), ``execute_to_table`` (materialized), and
``execute_to_reader`` (RecordBatchReader). Each call goes to exactly one source
in its native dialect — that is the contract from ADR 004.

The two sources are wired with ``MemoryExecutor`` instances declared as
``kind='postgres'`` and ``kind='snowflake'`` so the example runs without any
infrastructure. In production you'd substitute the ADBC/driver executors with
the same ``kind`` strings; the calling code below does not change.

Usage (from repo root):
    python examples/05_api_postgres_vs_snowflake.py
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

import pyarrow as pa

from nexcraft.catalog.inmemory import InMemoryCatalog
from nexcraft.client import FedSQLClient
from nexcraft.connection.static import StaticConnectionProvider
from nexcraft.core.context import QueryContext
from nexcraft.core.descriptors import ConnectionHandle, SourceDescriptor
from nexcraft.errors import BudgetExceededError
from nexcraft.executors.memory import MemoryExecutor
from nexcraft.router import Router


# ---------------------------------------------------------------------------
# Dialect-specific SQL — same intent, different syntax. The point is to show
# nexcraft does NOT translate dialects: each string is sent verbatim to the
# source it targets, and the source's own planner does the work.
# ---------------------------------------------------------------------------
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
    PARTITION BY USER_ID
    ORDER BY ORDER_TS DESC
) = 1
""".strip()


# ---------------------------------------------------------------------------
# Stubbed source data so the script runs anywhere.
# ---------------------------------------------------------------------------
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


def build_cross_source_client() -> FedSQLClient:
    """Wire two sources of different ``kind`` behind one ``FedSQLClient``."""

    # MemoryExecutor reports kind='memory'; for the demo we wrap it so its
    # advertised kind matches the descriptor it's serving. Real executors
    # (Postgres, Snowflake) declare their kind directly.
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

    pg_executor = _Reskinned(
        MemoryExecutor(replies={POSTGRES_RECENT_USERS_SQL: _users_batch()}),
        "postgres",
    )
    sf_executor = _Reskinned(
        MemoryExecutor(replies={SNOWFLAKE_RECENT_ORDERS_SQL: _orders_batch()}),
        "snowflake",
    )

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

    provider = StaticConnectionProvider(
        {
            "prod_pg": ConnectionHandle(source_id="prod_pg", kind="postgres"),
            "warehouse": ConnectionHandle(source_id="warehouse", kind="snowflake"),
        }
    )

    router = Router(
        catalog=catalog,
        connection_provider=provider,
        executors={"postgres": pg_executor, "snowflake": sf_executor},
    )
    return FedSQLClient(router)


# ---------------------------------------------------------------------------
# 1. describe() — schema discovery without execution.
# ---------------------------------------------------------------------------
async def demo_describe(client: FedSQLClient) -> None:
    print("\n[1] describe(): get schema before pulling rows")
    pg_ctx = QueryContext(tenant_id="default", query_id="api-describe-pg")
    sf_ctx = QueryContext(tenant_id="default", query_id="api-describe-sf")

    pg_schema = await client.describe("prod_pg", POSTGRES_RECENT_USERS_SQL, pg_ctx)
    sf_schema = await client.describe("warehouse", SNOWFLAKE_RECENT_ORDERS_SQL, sf_ctx)
    print("  postgres  ->", [(f.name, str(f.type)) for f in pg_schema])
    print("  snowflake ->", [(f.name, str(f.type)) for f in sf_schema])


# ---------------------------------------------------------------------------
# 2. execute_to_table() — materialize the result. Good for small results.
# ---------------------------------------------------------------------------
async def demo_execute_to_table(client: FedSQLClient) -> None:
    print("\n[2] execute_to_table(): one round-trip, full materialization")
    pg_ctx = QueryContext(tenant_id="default", query_id="api-pg-table")
    pg_users = await client.execute_to_table(
        "prod_pg", POSTGRES_RECENT_USERS_SQL, pg_ctx
    )
    print("  postgres rows:", pg_users.num_rows)
    print("  postgres cols:", pg_users.column_names)
    print("  postgres data:", pg_users.to_pydict())


# ---------------------------------------------------------------------------
# 3. execute() — async iterator of RecordBatch. Cancellation- and budget-aware.
# ---------------------------------------------------------------------------
async def demo_execute_streaming(client: FedSQLClient) -> None:
    print("\n[3] execute(): stream batches with deadline + budget on the context")
    sf_ctx = QueryContext(
        tenant_id="default",
        query_id="api-sf-stream",
        deadline=datetime.now(timezone.utc) + timedelta(seconds=10),
        max_rows=1_000,
    )
    total_rows = 0
    schema = None
    async for batch in client.execute("warehouse", SNOWFLAKE_RECENT_ORDERS_SQL, sf_ctx):
        if schema is None:
            schema = batch.schema
        total_rows += batch.num_rows
    print(f"  snowflake streamed rows: {total_rows}")
    print(f"  snowflake schema fields: {[f.name for f in (schema or [])]}")


# ---------------------------------------------------------------------------
# 4. execute_to_reader() — synchronous RecordBatchReader. Hands off cleanly to
#    DuckDB / Polars / pandas / anything that consumes Arrow.
# ---------------------------------------------------------------------------
async def demo_execute_to_reader(client: FedSQLClient) -> None:
    print("\n[4] execute_to_reader(): RecordBatchReader for downstream Arrow consumers")
    pg_ctx = QueryContext(tenant_id="default", query_id="api-pg-reader")
    reader = await client.execute_to_reader(
        "prod_pg", POSTGRES_RECENT_USERS_SQL, pg_ctx
    )
    table = reader.read_all()
    print("  postgres reader -> Table rows:", table.num_rows)
    print("  postgres reader -> first row:", table.slice(0, 1).to_pydict())


# ---------------------------------------------------------------------------
# 5. Budget enforcement — proves the client honours QueryContext.max_rows even
#    when the source would happily return more.
# ---------------------------------------------------------------------------
async def demo_budget_enforcement(client: FedSQLClient) -> None:
    print("\n[5] BudgetExceededError when QueryContext.max_rows is too small")
    sf_ctx = QueryContext(
        tenant_id="default",
        query_id="api-sf-budget",
        max_rows=1,  # the canned reply has 3 rows
    )
    try:
        async for _ in client.execute(
            "warehouse", SNOWFLAKE_RECENT_ORDERS_SQL, sf_ctx
        ):
            pass
    except BudgetExceededError as exc:
        print(
            f"  snowflake -> BudgetExceededError(kind={exc.budget_kind!r}, "
            f"limit={exc.limit}, observed={exc.observed})"
        )


async def main() -> None:
    client = build_cross_source_client()
    await demo_describe(client)
    await demo_execute_to_table(client)
    await demo_execute_streaming(client)
    await demo_execute_to_reader(client)
    await demo_budget_enforcement(client)


if __name__ == "__main__":
    asyncio.run(main())
