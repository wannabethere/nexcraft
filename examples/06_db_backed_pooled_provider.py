"""
Example: DB-backed connections + per-kind pool config + tenant validation.

Shows how a host system (Genie-style ``ConnectionDetails`` table) plugs into
nexcraft:

  * ``ManagementStore``      → adapts the host's connections table.
  * ``DBCatalog``            → exposes those rows as ``SourceDescriptor`` to nexcraft.
  * ``PooledConnectionProvider`` → loads details on demand, validates tenant,
                                   resolves secrets, hands out pooled handles.
  * ``YamlPoolConfig``       → external per-kind pool sizing (operator-tunable).

To keep the example dependency-free we use a fake in-memory store + fake
``DriverPoolFactory`` that returns stub handles. In production swap in
``AsyncpgPoolFactory`` for ``kind='postgres'`` and a Snowflake equivalent.

Usage (from repo root):
    python examples/06_db_backed_pooled_provider.py
"""

from __future__ import annotations

import asyncio
import os
from typing import Mapping

from nexcraft.catalog.db import DBCatalog
from nexcraft.connection.management import (
    ConnectionDetails,
    EnvSecretResolver,
    InMemoryManagementStore,
)
from nexcraft.connection.pool_config import PoolConfig, YamlPoolConfig
from nexcraft.connection.pooled import (
    DriverPool,
    DriverPoolFactory,
    PooledConnectionHandle,
    PooledConnectionProvider,
)
from nexcraft.core.context import QueryContext
from nexcraft.errors import AuthenticationError


# ---------------------------------------------------------------------------
# Stand-in for an asyncpg / Snowflake pool. Counts opens/checkouts so we can
# show pool reuse from the print output.
# ---------------------------------------------------------------------------
class _DemoPool:
    def __init__(self, *, source_id: str, kind: str, sizing: PoolConfig) -> None:
        self._source_id = source_id
        self._kind = kind
        self._sizing = sizing
        self._opened = False
        self._checkouts = 0

    @property
    def kind(self) -> str:
        return self._kind

    async def acquire(self, ctx: QueryContext) -> PooledConnectionHandle:
        if not self._opened:
            print(
                f"  [pool {self._source_id}] opening pool "
                f"(min={self._sizing.min_size}, max={self._sizing.max_size}, "
                f"timeout={self._sizing.acquire_timeout_s}s, "
                f"extras={dict(self._sizing.extras)})"
            )
            self._opened = True
        self._checkouts += 1
        print(f"  [pool {self._source_id}] checkout #{self._checkouts}")
        return PooledConnectionHandle(
            source_id=self._source_id,
            kind=self._kind,
            raw=f"<fake-{self._kind}-conn>",
            _pool_id=self._source_id,
        )

    async def release(self, handle: PooledConnectionHandle) -> None:
        print(f"  [pool {self._source_id}] release")

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
            f"using config {dict(details.config)} + secrets-keys={list(secrets.keys())}"
        )
        return _DemoPool(
            source_id=details.source_id, kind=self._kind, sizing=pool_config
        )


# ---------------------------------------------------------------------------
# Pretend we read these rows from the ConnectionDetails / DataSources tables.
# ---------------------------------------------------------------------------
def build_management_store() -> InMemoryManagementStore:
    return InMemoryManagementStore(
        [
            ConnectionDetails(
                source_id="prod_pg",
                tenant_id="acme",
                kind="postgres",
                display_name="ACME Production Postgres",
                config={"host": "pg.acme.internal", "database": "prod", "user": "app"},
                secret_ref="env:PG_PROD_PASSWORD",
                tags={"team": "data"},
            ),
            ConnectionDetails(
                source_id="warehouse",
                tenant_id="acme",
                kind="snowflake",
                display_name="ACME Snowflake Warehouse",
                config={"account": "acme", "warehouse": "ANALYTICS_WH", "user": "etl"},
                secret_ref="env:SF_PASSWORD",
            ),
            ConnectionDetails(
                source_id="other_tenant_pg",
                tenant_id="other-corp",
                kind="postgres",
                display_name="Other Corp Postgres",
                config={"host": "pg.other.internal"},
            ),
        ]
    )


# Per-kind pool config — the file an operator hand-edits, NOT something stored
# in the management DB. Per-source overrides allowed for hot spots.
POOL_CONFIG_YAML = """
defaults:
  postgres:
    min_size: 2
    max_size: 20
    acquire_timeout_s: 5
    statement_cache_size: 1024     # asyncpg-specific; passes through extras
  snowflake:
    min_size: 1
    max_size: 8
    acquire_timeout_s: 10
overrides:
  prod_pg:
    min_size: 5
    max_size: 50
"""


async def main() -> None:
    os.environ.setdefault("PG_PROD_PASSWORD", "stub-pg-pw")
    os.environ.setdefault("SF_PASSWORD", "stub-sf-pw")

    store = build_management_store()
    catalog = DBCatalog(store)
    pool_config = YamlPoolConfig.from_string(POOL_CONFIG_YAML)
    provider = PooledConnectionProvider(
        store=store,
        factories={
            "postgres": _DemoFactory("postgres"),
            "snowflake": _DemoFactory("snowflake"),
        },
        pool_config=pool_config,
        secrets=EnvSecretResolver(),
    )

    print("\n[1] Catalog: list sources for tenant 'acme' (DBCatalog → ManagementStore)")
    for src in await catalog.list_sources(tenant_id="acme"):
        print(f"  - {src.source_id} (kind={src.kind}, name={src.display_name})")

    print("\n[2] Acquire prod_pg twice → pool built once, two checkouts")
    ctx_acme = QueryContext(tenant_id="acme", query_id="q-1")
    h1 = await provider.acquire("prod_pg", ctx_acme)
    h2 = await provider.acquire("prod_pg", ctx_acme)
    await provider.release(h1)
    await provider.release(h2)

    print("\n[3] Acquire warehouse → second pool built, snowflake-specific sizing")
    h3 = await provider.acquire("warehouse", ctx_acme)
    await provider.release(h3)

    print("\n[4] Tenant boundary: tenant 'acme' tries to use 'other_tenant_pg'")
    ctx_acme_for_other = QueryContext(tenant_id="acme", query_id="q-2")
    try:
        await provider.acquire("other_tenant_pg", ctx_acme_for_other)
    except AuthenticationError as exc:
        print(f"  raised AuthenticationError as expected: {exc}")

    print("\n[5] Same pool reused — no new factory call")
    h4 = await provider.acquire("prod_pg", ctx_acme)
    await provider.release(h4)

    print("\n[6] Drain all pools on shutdown")
    await provider.close()


if __name__ == "__main__":
    asyncio.run(main())
