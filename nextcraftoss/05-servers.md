# 05 — Optional Servers

`nexcraft` is library-first. The default deployment is `import nexcraft` and call `client.execute(...)`. Some users want a service — for those, ship two thin servers on top of the same `Router`.

Both are opt-in extras: `pip install 'nexcraft[server]'`.

## Flight SQL server

Recommended primary. Arrow-native end-to-end.

### Why Flight SQL

- Arrow IPC on the wire — zero copy on localhost, columnar end-to-end across the network.
- Native streaming with HTTP/2 flow control (real backpressure).
- Bridge to JDBC via `flight-sql-jdbc-driver` covers BI tools without writing a JDBC server.
- Auth, TLS, and metadata propagation are first-class.

### Wire shape

Standard Flight SQL has a quirk: `CommandStatementQuery` only carries a SQL string. We need `source_id` and full `QueryContext` to ride along. Two options, recommended approach is the second:

#### Option A — Headers (works but stretches semantics)

Pack `source_id`, `tenant_id`, `query_id`, `deadline_ms` into gRPC metadata. Cheap. But Flight SQL expects one logical backend; using headers to switch sources is a workaround.

#### Option B — Custom `do_action` (recommended)

Define a `nexcraft.Execute` action with a protobuf payload, returning a Flight `Ticket`. Standard `do_get(ticket)` then streams the result.

```proto
syntax = "proto3";
package nexcraft.flight.v1;

message ExecuteRequest {
  string source_id   = 1;
  string sql         = 2;     // dialect-translated by caller
  string tenant_id   = 3;
  string query_id    = 4;
  string trace_id    = 5;
  int64  deadline_ms = 6;     // absolute epoch milliseconds
  optional int64 max_rows  = 7;
  optional int64 max_bytes = 8;
  int32  target_partitions = 9;
  int32  batch_size_hint   = 10;
  map<string, string> tags = 11;
}

message ExecuteTicket {
  bytes ticket_id = 1;        // opaque, server-side state key
}
```

This uses Flight as designed: `do_action` for control, `do_get` for streaming. Cleanly separates "what to run" from "stream me the result."

### Handler sketch

```python
import pyarrow.flight as fl
from nexcraft.client import Router
from nexcraft.core import QueryContext

class NexcraftFlightServer(fl.FlightServerBase):
    def __init__(self, router: Router, location: str):
        super().__init__(location)
        self._router = router
        self._tickets: dict[bytes, _PendingExecution] = {}

    def do_action(self, context, action):
        if action.type == "nexcraft.Execute":
            req = ExecuteRequest.FromString(action.body.to_pybytes())
            ticket_id = ulid.new().bytes
            ctx = self._build_query_context(req)
            self._tickets[ticket_id] = _PendingExecution(req, ctx)
            yield fl.Result(ExecuteTicket(ticket_id=ticket_id).SerializeToString())
        else:
            raise fl.FlightUnavailableError(f"unknown action: {action.type}")

    def do_get(self, context, ticket):
        pending = self._tickets.pop(ticket.ticket, None)
        if pending is None:
            raise fl.FlightCancelledError("ticket expired or unknown")

        async def run():
            stream = await self._router.execute(
                source_id=pending.req.source_id,
                sql=pending.req.sql,
                ctx=pending.ctx,
            )
            return stream

        stream = asyncio.run(run())  # or proper async bridge
        # Wrap as a FlightDataStream: schema first, then RecordBatches
        return self._to_flight_stream(stream)
```

Real implementation runs an async event loop alongside the gRPC server (e.g., via `aioflight` or running pyarrow's Flight server in a thread that bridges to asyncio).

### Auth

Two patterns, support both:

- **Bearer tokens** — JWT in `Authorization` metadata. Server validates and extracts `tenant_id`. Default for OSS.
- **mTLS** — peer certificate. Server extracts identity from cert subject. For zero-trust deployments.

Both feed into `tenant_id` resolution. The `ConnectionProvider` then resolves source credentials based on tenant.

### Cancellation

Flight propagates client disconnect / RPC cancellation. The server detects this and sets `ctx.cancel`. From there, normal cancellation flow runs (see [`04-streaming.md`](04-streaming.md)).

## HTTP server

For app integration, debugging, and clients that don't want gRPC.

### Endpoints

- `POST /v1/query` — execute a query.
  - Body: `{source_id, sql, max_rows?, max_bytes?, deadline_ms?, tags?}`
  - Headers: `Authorization`, optional `X-Tenant-Id`, `X-Query-Id`, `traceparent`.
  - Response:
    - If small (configurable threshold, default 10 MB): `Content-Type: application/json` with `{schema, rows}`.
    - If large or `Accept: application/vnd.apache.arrow.stream`: streamed Arrow IPC chunks.
- `GET /v1/sources` — list configured sources for the authenticated tenant.
- `GET /v1/sources/{source_id}` — get source descriptor.
- `POST /v1/describe` — same body as `/query`, returns `{schema}` only.
- `GET /v1/health` — liveness + per-executor readiness.

### Implementation

FastAPI + uvicorn. The handler is ~50 lines:

```python
from fastapi import FastAPI, Header, Request
from fastapi.responses import StreamingResponse
import pyarrow as pa, pyarrow.ipc as ipc

app = FastAPI()

@app.post("/v1/query")
async def query(body: QueryBody, request: Request,
                authorization: str = Header(...)):
    tenant_id = await auth.resolve_tenant(authorization)
    ctx = QueryContext(
        tenant_id=tenant_id,
        query_id=body.query_id or str(ulid.new()),
        deadline=body.deadline,
        max_rows=body.max_rows,
        max_bytes=body.max_bytes,
        cancel=asyncio.Event(),
    )

    # Wire client-disconnect to ctx.cancel
    asyncio.create_task(_watch_disconnect(request, ctx))

    stream = await router.execute(body.source_id, body.sql, ctx)

    if "application/vnd.apache.arrow.stream" in request.headers.get("accept", ""):
        return StreamingResponse(
            _arrow_ipc_chunks(stream),
            media_type="application/vnd.apache.arrow.stream",
        )
    # Small-result JSON path
    table = await collect_with_cap(stream, ctx.max_bytes or DEFAULT_JSON_CAP)
    return JSONResponse({"schema": table.schema.to_string(), "rows": table.to_pylist()})


async def _arrow_ipc_chunks(stream):
    sink = pa.BufferOutputStream()
    writer = None
    async for batch in stream:
        if writer is None:
            writer = ipc.new_stream(sink, batch.schema)
        writer.write_batch(batch)
        chunk = sink.getvalue().to_pybytes()
        sink = pa.BufferOutputStream()      # reset
        yield chunk
    if writer:
        writer.close()
        yield sink.getvalue().to_pybytes()
```

### Streaming etiquette

- HTTP/1.1 chunked transfer encoding for Arrow IPC streams.
- Don't buffer the whole result in the JSON path — cap and reject early via `BudgetExceededError`.
- Backpressure works through the underlying TCP write buffer; the queue in `CancellableArrowStream` provides the upstream backpressure.

## Configuration

Both servers take the same YAML config:

```yaml
nexcraft:
  bind: 0.0.0.0:50051                 # flight; or 8080 for http
  tls:
    cert: /etc/nexcraft/tls.crt
    key: /etc/nexcraft/tls.key
  auth:
    kind: jwt
    jwks_url: https://auth.example.com/.well-known/jwks.json
    audience: nexcraft

  catalog:
    kind: yaml
    path: /etc/nexcraft/catalog.yaml

  connection_provider:
    kind: env_vars                    # production deployments override

  executors:
    enabled: [postgres, snowflake, iceberg]

  observability:
    otel_endpoint: http://otel-collector:4317
    log_level: info
    log_sql: false                    # opt-in for production
```

Run:

```bash
python -m nexcraft.server.flight --config /etc/nexcraft/server.yaml
python -m nexcraft.server.http   --config /etc/nexcraft/server.yaml
```

Same router under both. Same observability. Same auth. Different envelopes.
