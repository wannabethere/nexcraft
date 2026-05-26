import pyarrow as pa
import pytest

from nexcraft.catalog.inmemory import InMemoryCatalog
from nexcraft.client import FedSQLClient
from nexcraft.connection.static import StaticConnectionProvider
from nexcraft.core.context import QueryContext
from nexcraft.core.descriptors import ConnectionHandle, SourceDescriptor
from nexcraft.executors.memory import MemoryExecutor
from nexcraft.router import Router


@pytest.fixture
def sample_setup() -> FedSQLClient:
    executor = MemoryExecutor()
    source = SourceDescriptor(
        source_id="mem",
        kind="memory",
        display_name="Memory",
        tenant_id="default",
        config={},
    )
    catalog = InMemoryCatalog({"mem": source})
    handles = {"mem": ConnectionHandle(source_id="mem", kind="memory")}
    provider = StaticConnectionProvider(handles)
    router = Router(
        catalog=catalog,
        connection_provider=provider,
        executors={"memory": executor},
    )
    return FedSQLClient(router)


@pytest.mark.asyncio
async def test_execute_select_one(sample_setup: FedSQLClient) -> None:
    ctx = QueryContext(tenant_id="default", query_id="q1")
    table = await sample_setup.execute_to_table("mem", "SELECT 1", ctx)
    assert table.num_rows == 1
    assert table.column_names == ["one"]


@pytest.mark.asyncio
async def test_stream_guard_budget(sample_setup: FedSQLClient) -> None:
    from nexcraft.errors import BudgetExceededError

    ctx = QueryContext(tenant_id="default", query_id="q2", max_rows=0)
    with pytest.raises(BudgetExceededError):
        async for _ in sample_setup.execute("mem", "SELECT 1", ctx):
            pass


@pytest.mark.asyncio
async def test_custom_reply(sample_setup: FedSQLClient) -> None:
    batch = pa.RecordBatch.from_arrays([pa.array(["x"])], names=["letter"])
    ex = MemoryExecutor(replies={"SELECT letter FROM t": [batch]})
    source = SourceDescriptor(
        source_id="mem2",
        kind="memory",
        display_name="Memory",
        tenant_id="default",
        config={},
    )
    catalog = InMemoryCatalog({"mem2": source})
    router = Router(
        catalog=catalog,
        connection_provider=StaticConnectionProvider(
            {"mem2": ConnectionHandle(source_id="mem2", kind="memory")}
        ),
        executors={"memory": ex},
    )
    client = FedSQLClient(router)
    ctx = QueryContext(tenant_id="default", query_id="q3")
    table = await client.execute_to_table("mem2", "SELECT letter FROM t", ctx)
    assert table.to_pylist() == [{"letter": "x"}]
