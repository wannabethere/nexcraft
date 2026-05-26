"""
Example: federated SQL via FedSQLClient using the in-memory executor.

Usage (from repo root):
    python examples/01_federated_sql_memory.py
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from nexcraft.core.context import QueryContext

from demo_kit import DEMO_SOURCE_ID, DEMO_TENANT, build_demo_client


async def main() -> None:
    client = build_demo_client()
    ctx = QueryContext(tenant_id=DEMO_TENANT, query_id="example-query-1")
    sql = "SELECT region, revenue FROM sales ORDER BY region"
    table = await client.execute_to_table(DEMO_SOURCE_ID, sql, ctx)
    print(table.to_pydict())


if __name__ == "__main__":
    asyncio.run(main())
