# 06 — Observability

OSS users deploy `nexcraft` in environments you cannot see. Observability is how they trust it. First-class, not bolted on.

## OpenTelemetry traces

Every public entry point opens a span. Spans follow OTel semantic conventions where possible.

### Span hierarchy

```
nexcraft.client.execute             ← root span, attrs: source_id, kind, tenant_id, query_id
├── nexcraft.catalog.get_source     ← attrs: source_id
├── nexcraft.connection.acquire     ← attrs: source_id, kind
├── nexcraft.executor.{kind}.execute
│   ├── nexcraft.executor.{kind}.describe         (if called)
│   ├── nexcraft.source.{kind}.query              ← actual source-side timing
│   │   └── attrs: rows_returned, bytes_returned, partitions (if applicable)
│   └── nexcraft.stream.consume                   ← attrs: batches_yielded, total_rows
└── (close)
```

### Required span attributes on `nexcraft.client.execute`

- `nexcraft.source_id`
- `nexcraft.source_kind`
- `nexcraft.tenant_id`
- `nexcraft.query_id`
- `nexcraft.batch_size_hint`
- `nexcraft.target_partitions`
- `nexcraft.deadline_ms` (if set)
- `nexcraft.max_rows` (if set)
- `nexcraft.max_bytes` (if set)
- `nexcraft.outcome` — one of `success`, `cancelled`, `timeout`, `budget_exceeded`, `source_error`, `connection_error`, `internal_error`
- `nexcraft.batches_yielded`
- `nexcraft.rows_yielded`
- `nexcraft.bytes_yielded`
- `nexcraft.duration_ms`
- `nexcraft.first_batch_ms` — time-to-first-batch, the latency metric users care about

### What's deliberately not in spans

- **The SQL string.** May contain sensitive data. Off by default; opt-in via `log_sql: true`.
- **Connection credentials.** Never. The connection provider sees them; spans don't.
- **Result data.** Schemas only, never values.

## Metrics

Exported as both OTLP metrics and Prometheus (via `prometheus_client`).

### Counter metrics

- `nexcraft_queries_total{kind, outcome}` — total queries, partitioned by source kind and outcome.
- `nexcraft_batches_total{kind}` — RecordBatches yielded.
- `nexcraft_rows_total{kind}` — rows yielded.
- `nexcraft_bytes_total{kind}` — bytes yielded.
- `nexcraft_cancellations_total{kind, reason}` — reasons: `caller`, `deadline`, `budget_rows`, `budget_bytes`.

### Histogram metrics

- `nexcraft_query_duration_seconds{kind, outcome}` — wall-clock duration.
- `nexcraft_first_batch_seconds{kind}` — time-to-first-batch.
- `nexcraft_batch_size_rows{kind}` — distribution of RecordBatch sizes.

### Gauge metrics

- `nexcraft_active_queries{kind}` — currently executing.
- `nexcraft_executor_pool_acquired{kind}` — connection pool occupancy (if the connection provider exposes it).

### Why these specifically

These map directly to the questions operators actually ask:

- "Is the service healthy?" → `nexcraft_queries_total{outcome="success"}` rate vs error rates.
- "Why is the dashboard slow?" → `nexcraft_first_batch_seconds{kind="snowflake"}` p99.
- "Are users hitting limits?" → `nexcraft_cancellations_total{reason="budget_rows"}`.
- "Is one tenant misbehaving?" → tenant-id is *not* in metric labels (cardinality), but is in trace attributes — pivot via traces.

Tenant ID is intentionally excluded from metric labels. Cardinality with multi-tenant deployments is unbounded; metrics aren't the right tool. Use traces for per-tenant analysis.

## Structured logs

`structlog` for emitting JSON logs by default. Plain text with colors in dev mode.

### Log levels

- **DEBUG** — every batch yielded, internal state transitions. Off in production.
- **INFO** — query start, query end, source-side latency, batch count. Default in production.
- **WARNING** — degraded behavior — retries (if added later), unexpected schema mismatch, budget warnings.
- **ERROR** — terminal failures — connection errors, source errors, internal errors.

### Required fields on every log line

- `query_id`
- `tenant_id`
- `source_id`
- `source_kind`
- `trace_id` (if present)
- `outcome` (on terminal events)

### Sensitive content policy

- SQL is **off by default**. Opt-in via config (`log_sql: true`). Even then, a sanitizer pass runs to redact common patterns (`'...'` literals → `'?'`).
- Connection params, tokens, passwords — never logged. The connection provider receives them; logs see the resolved `source_id` only.
- Error messages from sources may contain values from the SQL. They're logged at WARNING/ERROR but tagged `nexcraft.source_message: true` so log pipelines can route or redact further.

## The `--debug-plan` mode

The single most-loved feature you can ship for an OSS query tool: a way to see exactly what the executor *would* do without running it.

```bash
nexcraft debug-plan --source-id prod_pg --sql "SELECT * FROM users WHERE active"
```

Output for a pass-through executor:

```
Source: prod_pg (kind=postgres)
Connection: resolved (host=db.internal, db=app, user=svc_nexcraft)
Dialect SQL (sent to source):
    SELECT * FROM users WHERE active

Result schema (from PREPARE):
    id          INT64 NOT NULL
    email       UTF8
    active      BOOL
    created_at  TIMESTAMP[us, tz=UTC]

Cancellation: pg_cancel_backend on aux connection
Estimated cost: not available (Postgres EXPLAIN not run)
```

For a DataFusion-native executor:

```
Source: lake_iceberg (kind=iceberg)
Catalog: REST (https://nessie.internal)
Tables registered: warehouse.events
Logical plan:
    Projection: events.user_id, events.action, events.ts
      Filter: events.ts > '2024-01-01'
        TableScan: events partitioned by [date], pushdowns=[ts > ?]

Pushed predicates: ts > 2024-01-01
Pushed projections: [user_id, action, ts]
Estimated files scanned: 47 of 312
```

Implemented as a CLI subcommand and an SDK method (`client.debug_plan(...)`) returning a structured object.

## Logging configuration

```yaml
observability:
  otel:
    endpoint: http://otel-collector:4317
    sample_rate: 1.0                  # fraction of traces to sample
    resource:
      service.name: nexcraft
      service.version: 0.1.0
      deployment.environment: production
  prometheus:
    bind: 0.0.0.0:9090
    path: /metrics
  logs:
    format: json                      # json | console
    level: info
    log_sql: false
```
