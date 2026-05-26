"""Smoke tests for the Flight server. Uses nexcraft's MemoryExecutor so no
real Postgres / Snowflake / S3 is needed."""
from __future__ import annotations

import socket
import threading
import time
from typing import Iterator

import pyarrow as pa
import pytest

from nexcraft.catalog.inmemory import InMemoryCatalog
from nexcraft.client import FedSQLClient
from nexcraft.connection.static import StaticConnectionProvider
from nexcraft.core.descriptors import ConnectionHandle, SourceDescriptor
from nexcraft.executors.memory import MemoryExecutor
from nexcraft.router import Router

from nexcraft_driver.async_store import InProcessAsyncQueryStore
from nexcraft_driver.auth import AuthMiddlewareFactory
from nexcraft_driver.client import DriverClient
from nexcraft_driver.server import DriverFlightServer
from nexcraft_driver.types import QueryState


# --- Fixtures ----------------------------------------------------------------

def _free_port() -> int:
    s = socket.socket(); s.bind(("", 0))
    try:
        return s.getsockname()[1]
    finally:
        s.close()


SAMPLE_SQL = "SELECT * FROM events"
SAMPLE_BATCHES = [
    pa.RecordBatch.from_arrays(
        [pa.array(["u1", "u2", "u3"]), pa.array([10, 20, 30], type=pa.int64())],
        names=["user_id", "value"],
    )
]


@pytest.fixture
def server(tmp_path) -> Iterator[tuple[DriverFlightServer, int, str]]:
    """In-process FlightServer backed by MemoryExecutor + InProcess store."""
    executor = MemoryExecutor(replies={SAMPLE_SQL: SAMPLE_BATCHES})
    descriptor = SourceDescriptor(
        source_id="mem", kind="memory",
        display_name="memory", tenant_id="default", config={},
    )
    router = Router(
        catalog=InMemoryCatalog({"mem": descriptor}),
        connection_provider=StaticConnectionProvider(
            {"mem": ConnectionHandle(source_id="mem", kind="memory")}
        ),
        executors={"memory": executor},
    )
    fedsql = FedSQLClient(router)
    store  = InProcessAsyncQueryStore(fedsql, spool_dir=tmp_path / "spool")

    port = _free_port()
    location = f"grpc://127.0.0.1:{port}"
    srv = DriverFlightServer(
        fedsql=fedsql, store=store,
        auth=AuthMiddlewareFactory(insecure=True),
        location=location,
    )
    t = threading.Thread(target=srv.serve, daemon=True)
    t.start()
    # Tiny grace so the gRPC listener is bound before tests connect.
    time.sleep(0.2)
    try:
        yield srv, port, location
    finally:
        srv.shutdown()
        t.join(timeout=5)


@pytest.fixture
def client(server) -> Iterator[DriverClient]:
    _, _, location = server
    c = DriverClient(location)
    try:
        yield c
    finally:
        c.close()


# --- Sync path --------------------------------------------------------------

def test_execute_sync_returns_canned_data(client) -> None:
    tbl = client.execute_sync("mem", SAMPLE_SQL)
    assert tbl.num_rows == 3
    assert tbl.column_names == ["user_id", "value"]
    assert tbl.column("value").to_pylist() == [10, 20, 30]


# --- Async path -------------------------------------------------------------

def test_submit_poll_fetch_succeeds(client) -> None:
    handle = client.submit("mem", SAMPLE_SQL)
    # Poll until terminal.
    for _ in range(50):
        st = client.status(handle)
        if st.state in (QueryState.SUCCEEDED, QueryState.FAILED, QueryState.CANCELLED):
            break
        time.sleep(0.05)
    assert st.state is QueryState.SUCCEEDED
    assert st.rows == 3
    assert st.result_uri and st.result_uri.endswith(".parquet")

    tbl = client.fetch(handle)
    assert tbl.num_rows == 3
    assert tbl.column("user_id").to_pylist() == ["u1", "u2", "u3"]


def test_status_unknown_handle_is_expired(client) -> None:
    from nexcraft_driver.types import QueryHandle
    fake = QueryHandle(query_id="does-not-exist", submitted_at="2026-01-01T00:00:00Z")
    st = client.status(fake)
    assert st.state is QueryState.EXPIRED


def test_cancel_is_idempotent(client) -> None:
    handle = client.submit("mem", SAMPLE_SQL)
    client.cancel(handle)
    client.cancel(handle)  # second call should not raise
    # State is either cancelled (if cancellation won) or succeeded (if work
    # completed first) — either is a legal outcome for an in-memory store.
    st = client.status(handle)
    assert st.state in (QueryState.CANCELLED, QueryState.SUCCEEDED)


# --- Auth -------------------------------------------------------------------

def test_bearer_token_required_when_not_insecure(tmp_path) -> None:
    """Smoke that AuthMiddlewareFactory rejects missing Bearer header."""
    executor = MemoryExecutor(replies={SAMPLE_SQL: SAMPLE_BATCHES})
    descriptor = SourceDescriptor(
        source_id="mem", kind="memory",
        display_name="memory", tenant_id="default", config={},
    )
    router = Router(
        catalog=InMemoryCatalog({"mem": descriptor}),
        connection_provider=StaticConnectionProvider(
            {"mem": ConnectionHandle(source_id="mem", kind="memory")}
        ),
        executors={"memory": executor},
    )
    fedsql = FedSQLClient(router)
    store  = InProcessAsyncQueryStore(fedsql, spool_dir=tmp_path / "spool")
    port = _free_port()
    location = f"grpc://127.0.0.1:{port}"
    srv = DriverFlightServer(
        fedsql=fedsql, store=store,
        auth=AuthMiddlewareFactory(allowed_tokens=["s3cret"]),
        location=location,
    )
    t = threading.Thread(target=srv.serve, daemon=True)
    t.start()
    time.sleep(0.2)
    try:
        import pyarrow.flight as fl
        no_auth_client = DriverClient(location)
        with pytest.raises(fl.FlightUnauthenticatedError):
            no_auth_client.execute_sync("mem", SAMPLE_SQL)

        good = DriverClient(location, bearer_token="s3cret")
        tbl = good.execute_sync("mem", SAMPLE_SQL)
        assert tbl.num_rows == 3
    finally:
        srv.shutdown()
        t.join(timeout=5)
