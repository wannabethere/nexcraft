"""Thin client wrapper around `pyarrow.flight.FlightClient` for the four
custom actions the driver exposes. Used by `genieml/agents` and by tests.

All methods are sync because pyarrow.flight is sync; callers in async code
should run them in a thread (`asyncio.to_thread`).
"""
from __future__ import annotations

import json
from typing import Optional

import pyarrow as pa
import pyarrow.flight as fl

from nexcraft_driver.server import DriverFlightServer
from nexcraft_driver.types import QueryHandle, QueryStatus, SubmitRequest


class DriverClient:
    def __init__(self, location: str, *, bearer_token: Optional[str] = None) -> None:
        self._client = fl.FlightClient(location)
        self._options = fl.FlightCallOptions(
            headers=[(b"authorization", f"Bearer {bearer_token}".encode())]
            if bearer_token else None,
        )

    # --- sync execution ---------------------------------------------------

    def execute_sync(self, source_id: str, sql: str) -> pa.Table:
        """Block until the source returns the full result. Use for queries you
        expect to finish in seconds."""
        descriptor = fl.FlightDescriptor.for_command(
            json.dumps({"source_id": source_id, "sql": sql}).encode()
        )
        info = self._client.get_flight_info(descriptor, options=self._options)
        endpoint = info.endpoints[0]
        reader = self._client.do_get(endpoint.ticket, options=self._options)
        return reader.read_all()

    # --- async submission -------------------------------------------------

    def submit(self, source_id: str, sql: str, *, tenant_id: str = "default",
               deadline_seconds: Optional[int] = None) -> QueryHandle:
        req = SubmitRequest(source_id=source_id, sql=sql, tenant_id=tenant_id,
                            deadline_seconds=deadline_seconds)
        results = list(self._client.do_action(
            fl.Action(DriverFlightServer.SUBMIT_ACTION, req.to_bytes()),
            options=self._options,
        ))
        return QueryHandle.from_bytes(results[0].body.to_pybytes())

    def status(self, handle: QueryHandle) -> QueryStatus:
        results = list(self._client.do_action(
            fl.Action(DriverFlightServer.STATUS_ACTION, handle.to_bytes()),
            options=self._options,
        ))
        return QueryStatus.from_bytes(results[0].body.to_pybytes())

    def fetch(self, handle: QueryHandle) -> pa.Table:
        """Returns the full result table. The driver streams from Parquet — for
        large results, pull row-group at a time via `fetch_reader` instead."""
        results = list(self._client.do_action(
            fl.Action(DriverFlightServer.FETCH_ACTION, handle.to_bytes()),
            options=self._options,
        ))
        ticket = fl.Ticket(results[0].body.to_pybytes())
        return self._client.do_get(ticket, options=self._options).read_all()

    def cancel(self, handle: QueryHandle) -> None:
        list(self._client.do_action(
            fl.Action(DriverFlightServer.CANCEL_ACTION, handle.to_bytes()),
            options=self._options,
        ))

    def close(self) -> None:
        self._client.close()
