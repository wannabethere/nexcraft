from __future__ import annotations

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
def client_with_replies() -> FedSQLClient:
    batch = pa.RecordBatch.from_arrays(
        [pa.array([1, 2, 3], type=pa.int64()), pa.array(["a", "b", "c"])],
        names=["id", "label"],
    )
    executor = MemoryExecutor(replies={"SELECT id, label FROM t": [batch]})
    source = SourceDescriptor(
        source_id="mem",
        kind="memory",
        display_name="Memory",
        tenant_id="default",
        config={},
    )
    catalog = InMemoryCatalog({"mem": source})
    router = Router(
        catalog=catalog,
        connection_provider=StaticConnectionProvider(
            {"mem": ConnectionHandle(source_id="mem", kind="memory")}
        ),
        executors={"memory": executor},
    )
    return FedSQLClient(router)


@pytest.mark.asyncio
async def test_execute_to_reader_yields_record_batch_reader(
    client_with_replies: FedSQLClient,
) -> None:
    ctx = QueryContext(tenant_id="default", query_id="q1")
    reader = await client_with_replies.execute_to_reader(
        "mem", "SELECT id, label FROM t", ctx
    )
    assert isinstance(reader, pa.RecordBatchReader)
    table = reader.read_all()
    assert table.column_names == ["id", "label"]
    assert table.column("id").to_pylist() == [1, 2, 3]
    assert table.column("label").to_pylist() == ["a", "b", "c"]
