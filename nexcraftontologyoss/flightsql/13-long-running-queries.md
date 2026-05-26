# 13 — Long-Running Queries

Some analytical queries take minutes. The synchronous streaming path from [`05-servers.md`](05-servers.md) and [`10-driver-worker.md`](10-driver-worker.md) handles short and medium queries well, but breaks down at the multi-minute scale. This document specifies the failure modes, the threshold for sync vs async, the async submission path, and how it integrates with the existing platform.

## Position

Three principles:

1. **Sync is the default path.** Sub-second to minute-long queries flow through the existing driver → worker → stream path. This is the bulk of traffic and shouldn't get more complicated.
2. **Long queries get an explicit async path.** Callers who know their query is long opt into async submission. They get a handle immediately; results are spooled to object storage; they fetch when ready. No sync surprises.
3. **Auto-promotion is out of scope.** The driver does not guess. Callers declare. Predicting query duration from cardinality estimates is its own research problem, and getting it wrong is worse than asking.

## What breaks at multi-minute scale

The sync path assumes a continuous network connection from client to source, held open for the duration of the query. At ~5 minutes this breaks in five places:

### Load balancer idle timeouts
Default idle timeout on AWS ALB is 60 seconds, Google Cloud Load Balancer is 30 seconds, Azure Application Gateway is 4 minutes. Cloud LB defaults are shorter than long queries. The LB drops the connection mid-stream; the client sees a confusing "connection closed by upstream."

Streaming Arrow batches keeps the connection active because data flows continuously — but a query that takes 5 minutes before returning *any* rows (a heavy aggregation that scans before reducing) is silent for those 5 minutes from the LB's perspective. Dropped.

### Client-side query timeouts
Tableau Desktop defaults to 5-minute query timeout. Power BI DirectQuery defaults to 225 seconds (~4 minutes). The Apache Arrow Flight SQL JDBC driver respects the `QueryTimeout` JDBC property, default 0 (unlimited) but most clients set something. BI tools assume "if it hasn't returned in 5 minutes it's hung."

The client gives up and closes its side. Cancellation propagates (correctly, per design) and the source query is killed. The user sees "timeout"; the work is wasted.

### Driver memory and connection pinning
The driver proxies the worker's stream to the client. A 5-minute query means a 5-minute pinned driver task: holds the worker connection, holds the client connection, holds the cache-buffer (if `cache_mode != off`).

With 100 concurrent 5-minute queries, that's 100 pinned tasks. Workable but the driver's memory and connection budgets need headroom for the worst case.

### Worker source-connection pinning
Workers hold the source connection for the query's duration. Source connection pools size to the average; the long queries eat capacity disproportionately. A handful of 5-minute queries against Postgres can starve the pool for fast queries.

### Cache buffer memory
The current cache design buffers the whole result before writing to Redis on success. A 5-minute query producing 500 MB of results means the driver holds 500 MB in memory throughout. With the per-result cap at 100 MB, large long queries effectively can't be cached — which is fine, except it means the platform doesn't help these queries at all.

## The threshold

The boundary between "stay sync" and "go async" is at the **deadline**, not the actual duration. A query with `deadline=30s` stays sync regardless of how long the source thinks it'll take; a query with `deadline=15min` opts into async semantics.

Practical thresholds:

| Deadline | Path | Why |
|---|---|---|
| ≤ 60 seconds | Sync | Fits inside LB defaults; no infrastructure surprise. |
| 60s – 5 minutes | Sync, with caveats | Operator must configure LB idle timeout > deadline. BI tools work if their timeout is generous. |
| > 5 minutes | Async | The sync path stops being reliable. Opt into async submission. |

The threshold is configurable per driver instance:

```yaml
driver:
  sync_max_deadline_seconds: 300        # 5 minutes
  reject_sync_above_threshold: true     # if false, allow but warn
```

If `reject_sync_above_threshold` is true (recommended for production), the driver returns an error to callers attempting sync execution with deadlines above the threshold, pointing them at the async path.

## The async submission path

Two new Flight SQL actions (or HTTP endpoints) and a result storage shape.

### Submission

```
Client → Driver: SubmitQuery(source_id, sql, ctx with cache_mode/deadline/etc.)
Driver → Client: QueryHandle { query_id, submitted_at, expected_completion_estimate }
```

Driver:
1. Performs the same auth, catalog resolution, cache check, admission as for sync queries.
2. If cache hit: registers a "completed" query handle pointing at the cached result. Client polls and immediately gets the cached stream. Behaves like sync but through the async API.
3. If cache miss: dispatches to a worker as usual, but the worker writes the result to object storage (Parquet) instead of streaming back. Driver records the query in a metadata table.

The driver returns the handle immediately — within milliseconds. The client does not hold a long connection.

### Status polling

```
Client → Driver: GetQueryStatus(query_id)
Driver → Client: QueryStatus { state, progress, rows_so_far, bytes_so_far, error_if_failed }
```

States: `pending`, `running`, `succeeded`, `failed`, `cancelled`.

Polling rate is the client's call. A reasonable default is 1Hz; the driver caps minimum poll interval to ~5Hz to avoid hammering.

### Result fetch

```
Client → Driver: FetchQueryResults(query_id, offset?, limit?)
Driver → Client: Arrow stream of the result (or paginated subset)
```

The driver reads the result Parquet from object storage and streams it back. If the result is large, the client can paginate via `offset`/`limit`. If small, it just streams the whole thing.

### Cancellation

```
Client → Driver: CancelQuery(query_id)
Driver → Worker: cancel signal (existing path)
Driver → Client: ack
```

Same cancellation mechanism as sync queries. The worker propagates to the source via `pg_cancel_backend`, `SYSTEM$CANCEL_QUERY`, etc. The Parquet write (if in progress) is abandoned.

### Result lifecycle

Spooled results live in object storage with a TTL:

```
s3://nexcraft-results/queries/{tenant_id}/{date}/{query_id}/
  ├── result.parquet
  └── metadata.json
```

Default TTL is 24 hours. Operators can configure longer; tenants can set per-query overrides. After TTL, results are deleted by a sweeper; the metadata row is marked `expired`.

Result reuse: if the same caller submits the same query within the TTL window (cache key match), the existing result is returned instead of re-running. This is effectively a long-TTL L0 cache for async queries — implemented through the same cache infrastructure with a longer max_ttl.

## How this integrates with the existing platform

The async path **reuses infrastructure that already exists** for `nexcraft-jobs`:

### `ResultStore` from `nexcraft-jobs/jobs/04-storage.md`

Already specified: Parquet on object storage, metadata in Postgres, per-tenant prefix IAM. The async query path writes to the same `ResultStore`.

Differences:
- Recipe results live under `s3://bucket/jobs/...`.
- Async query results live under `s3://bucket/queries/...`.
- Same code path, different prefix.

### Metadata table extension

The `job_runs` table from `jobs/04-storage.md` gets a sibling — or simply extends to cover async queries:

```sql
CREATE TABLE query_runs (
    query_id          TEXT PRIMARY KEY,
    tenant_id         TEXT NOT NULL,
    source_id         TEXT NOT NULL,
    sql_hash          TEXT NOT NULL,            -- for cache dedup
    submitted_at      TIMESTAMPTZ NOT NULL,
    started_at        TIMESTAMPTZ,
    completed_at      TIMESTAMPTZ,
    state             TEXT NOT NULL,            -- pending|running|succeeded|failed|cancelled|expired
    worker_id         TEXT,                     -- which worker is running it
    result_uri        TEXT,                     -- s3://bucket/queries/...
    rows              BIGINT,
    bytes             BIGINT,
    error_class       TEXT,
    error_message     TEXT,
    expires_at        TIMESTAMPTZ NOT NULL,
    created_at        TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX idx_query_runs_tenant_submitted ON query_runs (tenant_id, submitted_at DESC);
CREATE INDEX idx_query_runs_state ON query_runs (state) WHERE state IN ('pending', 'running');
CREATE INDEX idx_query_runs_sql_hash ON query_runs (tenant_id, source_id, sql_hash) WHERE state = 'succeeded';
```

Could share schema with `job_runs` (one table, `kind` column distinguishing `recipe` vs `query`). Probably cleaner as two tables given the lifecycle differences. v0.1: separate tables.

### Worker changes

Workers gain one new capability: stream-to-Parquet instead of stream-to-driver.

```python
class Worker:
    async def execute_async(self, request: WorkerExecuteAsyncRequest):
        executor, conn = await self._resolve(request)
        stream = executor.execute(request.sql, request.ctx, conn)

        # Stream to Parquet on object storage instead of back to driver
        path = f"s3://{request.result_bucket}/{request.result_key}"
        writer = pa.parquet.ParquetWriter(path, stream.schema)
        rows = 0
        bytes_ = 0

        async for batch in stream:
            writer.write_batch(batch)
            rows += batch.num_rows
            bytes_ += batch.nbytes
            # Heartbeat with progress
            await self._report_progress(request.query_id, rows, bytes_)

        writer.close()
        return WorkerExecuteAsyncResult(
            result_uri=path,
            row_count=rows,
            byte_count=bytes_,
        )
```

The existing `SourceExecutor` is unchanged. The async worker capability is a wrapper around the same executor that streams to a different sink.

Progress heartbeats go to Redis (`query_progress:{query_id}` with TTL), so the driver can answer `GetQueryStatus` cheaply.

### Driver changes

The driver gains:

1. **Submission handler** — translates `SubmitQuery` into worker dispatch with async sink.
2. **Status handler** — reads from Redis progress key and the `query_runs` row.
3. **Fetch handler** — streams from object storage; uses the same `CancellableArrowStream` primitive.
4. **Sweeper** — background job to delete expired results and mark `query_runs` rows.

All four are bounded additions; no existing handler changes.

## Affinity to `nexcraft-jobs`

The line between "async query" and "recipe" gets blurry. Where's the right boundary?

| Concern | Async query | Recipe (`nexcraft-jobs`) |
|---|---|---|
| Author | Caller-submitted SQL | Pre-registered Python class |
| Complexity | One SQL statement | Multi-step pipeline |
| Retries | None (rerun manually) | Built into Temporal workflow |
| Compute | Source-side | DuckDB after extract |
| Scheduling | Not scheduled (submitted) | Optional Temporal Cron |
| Storage | Parquet result | Parquet result(s) + metadata |
| API | Submit / Poll / Fetch | Submit / Poll / Fetch |
| Authoring effort | None | Python class |
| Lifecycle visibility | Status field | Temporal UI |
| When to use | "This query takes 10 minutes" | "Daily aggregation across sources" |

The rule of thumb:

- **Async query**: single SQL statement that's slow. Single-source. Ad-hoc. "I want my dashboard refresh to handle this slow query."
- **Recipe**: multi-step analytical pipeline. Multi-source. Pre-defined. "Compute daily variance reports."

When a query crosses into "needs cross-source data" or "needs Python compute," it becomes a recipe. When it's "one SQL, just slow," it stays an async query.

For most callers, the distinction is "I'm writing this SQL right now" (async query) vs "I'm writing a Python class" (recipe).

## BI tool implications

BI tools don't natively support async query submission. Three mitigation paths:

### Mitigation 1 — Stay sync, configure timeouts generously

For queries the operator knows are slow but tolerable (60s–5min):

- Set Tableau's query timeout to 10 minutes.
- Set LB idle timeout above the query deadline.
- Use `cache_mode=stale_while_revalidate` so user-visible latency stays low: the BI tool gets cached data immediately, refresh happens in the background async.

This is the recommended path for most slow-but-not-super-long queries.

### Mitigation 2 — Materialize to a recipe

For predictable long queries — daily aggregations, weekly rollups — author them as `nexcraft-jobs` recipes. The recipe runs on schedule; the BI tool reads the materialized Parquet result via `nexcraft` as a fast source.

This is the right answer for the dashboard-query case where the BI tool keeps asking for the same aggregation. The 10-minute query becomes a 50ms query because it reads pre-computed results.

### Mitigation 3 — Async submission via custom connector

Power BI custom connectors (Power Query M) support pagination patterns that map to async submission. The connector calls `SubmitQuery`, polls `GetQueryStatus`, fetches results when ready. The user sees "refreshing..." for 5 minutes; Power BI doesn't drop the operation because there's no held connection.

Tableau doesn't directly support this pattern via JDBC. Working around requires either a custom Tableau Connector SDK package or a recipe-materialization approach (Mitigation 2).

### Recommendation

**For v0.1, document all three.** Recipe materialization is the most powerful for repeat queries. Stale-while-revalidate is the best for slow-but-tolerable. Direct async submission is a power user feature reachable from agents and custom integrations, not from BI tools.

## Cache implications

The L0 cache from `11-caching.md` mostly works for async queries with one wrinkle:

- **Cache the result, not the streaming bytes.** The cache key includes `(source_id, normalized_sql, tenant_id, principal)` — the same as sync. The cache value points at the result Parquet URI instead of holding the bytes inline.
- **Larger max_cacheable_bytes.** The 100 MB cap was reasonable for in-memory caching; with Parquet-on-object-storage, multi-GB results can be cached effectively. A separate `max_cacheable_async_result_bytes` knob (default 10 GB).
- **Longer TTL for async queries.** Sync default is 5 minutes; async default is 1 hour (because if you spent 5 minutes computing it, you want to amortize).

Cache hit path for async:
- `SubmitQuery` checks cache. If hit, returns a `QueryHandle` immediately marking `state=succeeded` with the cached result URI.
- Client polls once, sees `succeeded`, fetches results. End-to-end latency is roughly the cost of one round trip.

This effectively makes async queries lazy: a repeated 5-minute query returns instantly.

## Per-tenant quotas

The quota model from `10-driver-worker.md` extends:

```python
@dataclass
class TenantQuota:
    # ... existing fields ...
    max_concurrent_async_queries: int = 5
    max_async_result_bytes: int = 50 * 1024 * 1024 * 1024   # 50 GB
    max_async_queries_per_hour: int | None = None
    max_async_result_retention: timedelta = timedelta(hours=24)
```

Async queries consume different resources than sync queries; the quotas are tracked separately. A tenant maxed out on sync concurrency can still submit async (and vice versa).

## Observability

New metrics:

- `nexcraft_async_queries_submitted_total{source_kind}` — submission rate.
- `nexcraft_async_queries_state_total{state}` — current state distribution (pending/running/succeeded/failed/cancelled).
- `nexcraft_async_query_duration_seconds{source_kind, outcome}` — submit-to-completion histogram.
- `nexcraft_async_result_bytes{source_kind}` — distribution of result sizes.
- `nexcraft_async_result_storage_bytes` — gauge of total bytes in result storage.
- `nexcraft_async_result_fetches_total{state}` — fetch rate.

New OTel spans:
- `nexcraft.async.submit` — submission.
- `nexcraft.async.execute` — long-lived span tracking the actual execution (may be very long).
- `nexcraft.async.fetch` — result fetch.

The audit log gains async event types: `async_query.submit`, `async_query.complete`, `async_query.cancel`, `async_query.fetch`. Same record shape with an additional `async_query_id` field.

## What v0.1 ships

| Deliverable | Where |
|---|---|
| `SubmitQuery`, `GetQueryStatus`, `FetchQueryResults`, `CancelQuery` Flight SQL actions | Driver |
| HTTP equivalents (`POST /v1/queries`, `GET /v1/queries/{id}`, `GET /v1/queries/{id}/results`) | Driver |
| `query_runs` Postgres table + migrations | Metadata DB |
| Worker async execution path (stream to Parquet) | Workers |
| Sweeper for expired result cleanup | Driver background task |
| Per-tenant async quotas | Driver admission |
| Reuse of `ResultStore` infrastructure from `nexcraft-jobs` | `nexcraft.store` shared |
| `cache_mode` support for async queries | Driver |
| Configurable `sync_max_deadline_seconds` threshold | Driver config |
| Sample agent code calling the async API | `examples/async-query/` |
| `how-to/async-queries.md` | User docs |

## What v0.1 does NOT ship

- Auto-promotion (driver auto-converts sync to async based on prediction). Callers declare.
- Cross-region result replication.
- Streaming partial results (the result is one Parquet; pagination is over rows, not over time).
- Result format other than Parquet (no CSV, no JSON for async — clients fetch and convert).
- Webhook-based completion notification (poll only). Webhooks are v0.2.
- Result-to-result chaining ("use this query's result as input to that query"). That's what recipes are for.

## Operational checklist

Before relying on async queries in production:

- [ ] Object storage configured with per-tenant prefix IAM (same as `nexcraft-jobs`).
- [ ] `query_runs` table migrated; sweeper running.
- [ ] Per-tenant async quotas configured via `TenantQuotaProvider`.
- [ ] LB idle timeouts configured above `sync_max_deadline_seconds`.
- [ ] BI tool integrations documented for which mitigation path applies.
- [ ] Operators understand the async vs recipe boundary.
- [ ] Sweeper schedule does not collide with peak traffic.
- [ ] Cache TTLs for async queries reviewed (longer than sync).
- [ ] Cancellation tested end-to-end: client cancel → driver → worker → source-side cancel verified.
- [ ] Sample async clients tested with the agent and custom-connector flows.

This list goes in the `how-to/operate-async-queries.md` doc on the user-facing site.
