from __future__ import annotations

import asyncio
from typing import Mapping

import pytest

from nexcraft.connection.management import (
    ConnectionDetails,
    EnvSecretResolver,
    InMemoryManagementStore,
)
from nexcraft.connection.pool_config import PoolConfig, StaticPoolConfig, YamlPoolConfig
from nexcraft.connection.pooled import (
    DriverPool,
    DriverPoolFactory,
    PooledConnectionHandle,
    PooledConnectionProvider,
)
from nexcraft.core.context import QueryContext
from nexcraft.errors import AuthenticationError, ConfigurationError


# ---------------------------------------------------------------------------
# Fakes — let us test the provider without needing a real database.
# ---------------------------------------------------------------------------
class _FakePool:
    def __init__(self, *, source_id: str, kind: str, sizing: PoolConfig) -> None:
        self._source_id = source_id
        self._kind = kind
        self.sizing = sizing
        self.acquire_calls = 0
        self.release_calls = 0
        self.closed = False
        self.outstanding = 0

    @property
    def kind(self) -> str:
        return self._kind

    async def acquire(self, ctx: QueryContext) -> PooledConnectionHandle:
        self.acquire_calls += 1
        self.outstanding += 1
        return PooledConnectionHandle(
            source_id=self._source_id,
            kind=self._kind,
            raw=object(),
            _pool_id=self._source_id,
        )

    async def release(self, handle: PooledConnectionHandle) -> None:
        self.release_calls += 1
        self.outstanding -= 1

    async def close(self) -> None:
        self.closed = True


class _FakeFactory:
    def __init__(self, *, kind: str) -> None:
        self._kind = kind
        self.create_calls = 0
        self.last_secrets: Mapping[str, str] | None = None
        self.last_sizing: PoolConfig | None = None

    async def create(
        self, *, details, secrets, pool_config: PoolConfig
    ) -> DriverPool:
        self.create_calls += 1
        self.last_secrets = secrets
        self.last_sizing = pool_config
        return _FakePool(
            source_id=details.source_id, kind=self._kind, sizing=pool_config
        )


def _ctx(tenant: str = "tenant-a") -> QueryContext:
    return QueryContext(tenant_id=tenant, query_id=f"{tenant}-q1")


# ---------------------------------------------------------------------------
# Tenant validation
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_acquire_rejects_cross_tenant_access() -> None:
    store = InMemoryManagementStore(
        [
            ConnectionDetails(
                source_id="prod_pg",
                tenant_id="tenant-a",
                kind="postgres",
                display_name="A's Postgres",
                config={"host": "pg.a"},
            )
        ]
    )
    provider = PooledConnectionProvider(
        store=store, factories={"postgres": _FakeFactory(kind="postgres")}
    )
    with pytest.raises(AuthenticationError):
        await provider.acquire("prod_pg", _ctx(tenant="tenant-b"))


@pytest.mark.asyncio
async def test_acquire_succeeds_when_tenant_matches() -> None:
    store = InMemoryManagementStore(
        [
            ConnectionDetails(
                source_id="prod_pg",
                tenant_id="tenant-a",
                kind="postgres",
                display_name="A's Postgres",
                config={"host": "pg.a"},
            )
        ]
    )
    factory = _FakeFactory(kind="postgres")
    provider = PooledConnectionProvider(store=store, factories={"postgres": factory})
    handle = await provider.acquire("prod_pg", _ctx(tenant="tenant-a"))
    assert isinstance(handle, PooledConnectionHandle)
    assert handle.kind == "postgres"
    assert factory.create_calls == 1


# ---------------------------------------------------------------------------
# Pool reuse + per-kind sizing
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_pool_is_built_once_per_source() -> None:
    store = InMemoryManagementStore(
        [
            ConnectionDetails(
                source_id="prod_pg",
                tenant_id="t",
                kind="postgres",
                display_name="pg",
                config={"host": "pg"},
            )
        ]
    )
    factory = _FakeFactory(kind="postgres")
    provider = PooledConnectionProvider(store=store, factories={"postgres": factory})

    h1 = await provider.acquire("prod_pg", _ctx("t"))
    h2 = await provider.acquire("prod_pg", _ctx("t"))
    assert factory.create_calls == 1, "second acquire must reuse the pool"
    await provider.release(h1)
    await provider.release(h2)
    pool = list(provider._pools.values())[0]
    assert pool.acquire_calls == 2
    assert pool.release_calls == 2
    assert pool.outstanding == 0


@pytest.mark.asyncio
async def test_concurrent_first_acquires_build_one_pool() -> None:
    store = InMemoryManagementStore(
        [
            ConnectionDetails(
                source_id="prod_pg",
                tenant_id="t",
                kind="postgres",
                display_name="pg",
                config={"host": "pg"},
            )
        ]
    )
    factory = _FakeFactory(kind="postgres")
    provider = PooledConnectionProvider(store=store, factories={"postgres": factory})

    handles = await asyncio.gather(
        *(provider.acquire("prod_pg", _ctx("t")) for _ in range(10))
    )
    assert factory.create_calls == 1
    assert all(isinstance(h, PooledConnectionHandle) for h in handles)


@pytest.mark.asyncio
async def test_per_kind_pool_config_is_applied() -> None:
    store = InMemoryManagementStore(
        [
            ConnectionDetails(
                source_id="pg1",
                tenant_id="t",
                kind="postgres",
                display_name="pg",
                config={},
            ),
            ConnectionDetails(
                source_id="sf1",
                tenant_id="t",
                kind="snowflake",
                display_name="sf",
                config={},
            ),
        ]
    )
    pool_config = StaticPoolConfig(
        defaults={
            "postgres": PoolConfig(min_size=2, max_size=20),
            "snowflake": PoolConfig(min_size=1, max_size=4),
        }
    )
    pg_factory = _FakeFactory(kind="postgres")
    sf_factory = _FakeFactory(kind="snowflake")
    provider = PooledConnectionProvider(
        store=store,
        factories={"postgres": pg_factory, "snowflake": sf_factory},
        pool_config=pool_config,
    )

    await provider.acquire("pg1", _ctx("t"))
    await provider.acquire("sf1", _ctx("t"))
    assert pg_factory.last_sizing == PoolConfig(min_size=2, max_size=20)
    assert sf_factory.last_sizing == PoolConfig(min_size=1, max_size=4)


@pytest.mark.asyncio
async def test_per_source_override_beats_kind_default() -> None:
    store = InMemoryManagementStore(
        [
            ConnectionDetails(
                source_id="hot_pg",
                tenant_id="t",
                kind="postgres",
                display_name="hot",
                config={},
            )
        ]
    )
    pool_config = StaticPoolConfig(
        defaults={"postgres": PoolConfig(min_size=2, max_size=20)},
        overrides={"hot_pg": PoolConfig(min_size=10, max_size=100)},
    )
    factory = _FakeFactory(kind="postgres")
    provider = PooledConnectionProvider(
        store=store, factories={"postgres": factory}, pool_config=pool_config
    )
    await provider.acquire("hot_pg", _ctx("t"))
    assert factory.last_sizing == PoolConfig(min_size=10, max_size=100)


# ---------------------------------------------------------------------------
# Secrets + missing factories + close
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_secret_resolver_is_invoked(monkeypatch) -> None:
    monkeypatch.setenv("PG_PROD_PASSWORD", "hunter2")
    store = InMemoryManagementStore(
        [
            ConnectionDetails(
                source_id="prod_pg",
                tenant_id="t",
                kind="postgres",
                display_name="pg",
                config={"host": "pg.host", "user": "app"},
                secret_ref="env:PG_PROD_PASSWORD",
            )
        ]
    )
    factory = _FakeFactory(kind="postgres")
    provider = PooledConnectionProvider(
        store=store,
        factories={"postgres": factory},
        secrets=EnvSecretResolver(),
    )
    await provider.acquire("prod_pg", _ctx("t"))
    assert factory.last_secrets == {"password": "hunter2"}


@pytest.mark.asyncio
async def test_missing_factory_raises_configuration_error() -> None:
    store = InMemoryManagementStore(
        [
            ConnectionDetails(
                source_id="bq1",
                tenant_id="t",
                kind="bigquery",
                display_name="bq",
                config={},
            )
        ]
    )
    provider = PooledConnectionProvider(store=store, factories={})
    with pytest.raises(ConfigurationError):
        await provider.acquire("bq1", _ctx("t"))


@pytest.mark.asyncio
async def test_close_drains_all_pools() -> None:
    store = InMemoryManagementStore(
        [
            ConnectionDetails(
                source_id="a",
                tenant_id="t",
                kind="postgres",
                display_name="a",
                config={},
            ),
            ConnectionDetails(
                source_id="b",
                tenant_id="t",
                kind="postgres",
                display_name="b",
                config={},
            ),
        ]
    )
    factory = _FakeFactory(kind="postgres")
    provider = PooledConnectionProvider(store=store, factories={"postgres": factory})
    await provider.acquire("a", _ctx("t"))
    await provider.acquire("b", _ctx("t"))
    pools = list(provider._pools.values())
    await provider.close()
    assert all(p.closed for p in pools)
    assert provider._pools == {}


# ---------------------------------------------------------------------------
# YAML pool config
# ---------------------------------------------------------------------------
def test_yaml_pool_config_round_trip() -> None:
    yaml = pytest.importorskip("yaml")  # noqa: F841
    cfg = YamlPoolConfig.from_string(
        """
        defaults:
          postgres:
            min_size: 3
            max_size: 30
            statement_cache_size: 1024
          snowflake:
            min_size: 1
            max_size: 8
        overrides:
          hot_pg:
            min_size: 10
            max_size: 100
        """
    )
    pg = cfg.get(kind="postgres", source_id="anything")
    assert pg.min_size == 3 and pg.max_size == 30
    assert pg.extras == {"statement_cache_size": 1024}
    sf = cfg.get(kind="snowflake", source_id="anything")
    assert sf.max_size == 8
    hot = cfg.get(kind="postgres", source_id="hot_pg")
    assert hot.min_size == 10 and hot.max_size == 100
