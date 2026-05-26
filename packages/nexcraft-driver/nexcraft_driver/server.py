"""Flight gRPC driver server.

Wraps `FedSQLClient` from `nexcraft_driver.integration` with two surfaces:

1. **Sync query path** — a Flight descriptor of shape
   `{"action": "execute_sync", "source_id": "<src>", "sql": "..."}` returned via
   `get_flight_info`, then streamed via `do_get`.

2. **Async query actions** — four custom Flight actions delegating to an
   `AsyncQueryStore`:
     - `nexcraft.SubmitQuery`        (payload: SubmitRequest)         → QueryHandle
     - `nexcraft.GetQueryStatus`     (payload: QueryHandle)           → QueryStatus
     - `nexcraft.FetchQueryResults`  (payload: QueryHandle)           → Arrow stream via do_get
     - `nexcraft.CancelQuery`        (payload: QueryHandle)           → empty ack

This is a small subset of Flight SQL — we do NOT implement the BI-side
introspection actions (GetTables, GetSchemas, etc). Per design discussion: BI
tools are out of scope for this driver.

The class is constructed by `build_driver_server()` which assembles the
FedSQLClient from env vars (using `build_cross_source_fedsql`), wires an
in-process AsyncQueryStore, and returns a ready-to-serve `DriverFlightServer`.
"""
from __future__ import annotations

import asyncio
import json
import logging
import threading
from concurrent.futures import Future
from pathlib import Path
from typing import Any

import pyarrow as pa
import pyarrow.flight as fl

from nexcraft.client import FedSQLClient
from nexcraft.core.context import QueryContext

from nexcraft_driver.async_store import AsyncQueryStore, InProcessAsyncQueryStore
from nexcraft_driver.auth import AuthMiddlewareFactory, factory_from_env
from nexcraft_driver.integration import build_cross_source_fedsql
from nexcraft_driver.types import QueryHandle, QueryStatus, SubmitRequest

logger = logging.getLogger("nexcraft_driver.server")


# ---------------------------------------------------------------------------
# Loop bridge: Flight server callbacks are sync; our internals are async. Run
# a dedicated event loop in a background thread and submit coroutines to it.
# ---------------------------------------------------------------------------

class _LoopBridge:
    def __init__(self) -> None:
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(
            target=self._loop.run_forever, daemon=True, name="nexcraft-driver-loop"
        )
        self._thread.start()

    def run(self, coro) -> Any:
        fut: Future = asyncio.run_coroutine_threadsafe(coro, self._loop)
        return fut.result()

    def close(self) -> None:
        self._loop.call_soon_threadsafe(self._loop.stop)
        self._thread.join(timeout=5)


# ---------------------------------------------------------------------------
# Flight server
# ---------------------------------------------------------------------------

class DriverFlightServer(fl.FlightServerBase):
    """Flight gRPC server. Construct via `build_driver_server()`."""

    SUBMIT_ACTION = "nexcraft.SubmitQuery"
    STATUS_ACTION = "nexcraft.GetQueryStatus"
    FETCH_ACTION  = "nexcraft.FetchQueryResults"
    CANCEL_ACTION = "nexcraft.CancelQuery"

    SYNC_TICKET   = b"nexcraft.execute_sync"     # marker for sync-stream tickets
    ASYNC_TICKET  = b"nexcraft.fetch_async"      # marker for async-fetch tickets

    def __init__(
        self,
        *,
        fedsql: FedSQLClient,
        store: AsyncQueryStore,
        auth: AuthMiddlewareFactory,
        location: str = "grpc://0.0.0.0:50051",
    ) -> None:
        super().__init__(
            location,
            middleware={"auth": auth},
        )
        self._fedsql = fedsql
        self._store  = store
        self._bridge = _LoopBridge()

    # --- sync query path -------------------------------------------------

    def get_flight_info(self, context, descriptor):
        """Client builds a `FlightDescriptor.for_command(json_bytes)` where the
        JSON is `{"source_id": "...", "sql": "..."}`. We return a FlightInfo
        whose endpoint ticket is `{SYNC_TICKET}:<encoded_command>`."""
        cmd = json.loads(descriptor.command.decode("utf-8"))
        source_id = cmd["source_id"]
        sql       = cmd["sql"]
        # Cheap schema describe so the FlightInfo carries a real Arrow schema.
        # If a backend can't describe, we ship an empty schema; client will pick
        # it up from the streamed batches.
        try:
            qctx = QueryContext(tenant_id="default", query_id="describe")
            schema = self._bridge.run(self._fedsql.describe(source_id, sql, qctx))
        except Exception as exc:  # noqa: BLE001
            logger.warning("describe() failed for %s; returning empty schema (%s)", source_id, exc)
            schema = pa.schema([])
        ticket = fl.Ticket(self.SYNC_TICKET + b":" + descriptor.command)
        endpoint = fl.FlightEndpoint(ticket, [fl.Location.for_grpc_tcp("localhost", 50051)])
        return fl.FlightInfo(schema, descriptor, [endpoint], -1, -1)

    def do_get(self, context, ticket):
        """Stream Arrow batches for either a sync query or a previously-spooled
        async result, distinguished by ticket prefix."""
        raw = ticket.ticket
        if raw.startswith(self.SYNC_TICKET + b":"):
            cmd = json.loads(raw[len(self.SYNC_TICKET) + 1:].decode("utf-8"))
            return self._stream_sync(cmd["source_id"], cmd["sql"])
        if raw.startswith(self.ASYNC_TICKET + b":"):
            handle = QueryHandle.from_bytes(raw[len(self.ASYNC_TICKET) + 1:])
            return self._stream_async(handle)
        raise fl.FlightUnauthenticatedError(f"unknown ticket prefix: {raw[:32]!r}")

    def _stream_sync(self, source_id: str, sql: str) -> fl.RecordBatchStream:
        qctx = QueryContext(tenant_id="default", query_id="sync")
        table = self._bridge.run(self._fedsql.execute_to_table(source_id, sql, qctx))
        return fl.RecordBatchStream(table)

    def _stream_async(self, handle: QueryHandle) -> fl.GeneratorStream:
        async def collect() -> pa.Table:
            batches = []
            async for b in self._store.fetch(handle):
                batches.append(b)
            if not batches:
                return pa.Table.from_batches([], schema=pa.schema([]))
            return pa.Table.from_batches(batches)
        table = self._bridge.run(collect())
        return fl.RecordBatchStream(table)

    # --- async custom actions --------------------------------------------

    def list_actions(self, context):
        return [
            (self.SUBMIT_ACTION, "Submit a query for async execution"),
            (self.STATUS_ACTION, "Poll a previously-submitted query"),
            (self.FETCH_ACTION,  "Return a Flight ticket to fetch the result"),
            (self.CANCEL_ACTION, "Cancel a running query"),
        ]

    def do_action(self, context, action):
        body = action.body.to_pybytes() if hasattr(action.body, "to_pybytes") else bytes(action.body)
        if action.type == self.SUBMIT_ACTION:
            req = SubmitRequest.from_bytes(body)
            handle = self._bridge.run(self._store.submit(req))
            yield fl.Result(handle.to_bytes())
        elif action.type == self.STATUS_ACTION:
            handle = QueryHandle.from_bytes(body)
            status: QueryStatus = self._bridge.run(self._store.status(handle))
            yield fl.Result(status.to_bytes())
        elif action.type == self.FETCH_ACTION:
            # Returns a ticket the client passes back to do_get for streaming.
            handle = QueryHandle.from_bytes(body)
            ticket = self.ASYNC_TICKET + b":" + handle.to_bytes()
            yield fl.Result(ticket)
        elif action.type == self.CANCEL_ACTION:
            handle = QueryHandle.from_bytes(body)
            self._bridge.run(self._store.cancel(handle))
            yield fl.Result(b"")
        else:
            raise NotImplementedError(f"unknown action: {action.type!r}")

    # --- lifecycle -------------------------------------------------------

    def shutdown(self):
        try:
            self._bridge.run(self._store.aclose())
        finally:
            self._bridge.close()
            super().shutdown()


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------

async def build_driver_server(
    *,
    location: str = "grpc://0.0.0.0:50051",
    spool_dir: str | Path = "_async_results",
) -> DriverFlightServer:
    """Build a server using env-configured sources (whichever of
    Postgres / Snowflake / Delta / Iceberg have credentials)."""
    fedsql, _provider = await build_cross_source_fedsql()
    store = InProcessAsyncQueryStore(fedsql, spool_dir=spool_dir)
    auth  = factory_from_env()
    return DriverFlightServer(fedsql=fedsql, store=store, auth=auth, location=location)
