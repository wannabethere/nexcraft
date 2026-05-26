# Jobs 02 — Temporal Runtime

The recipe pattern is deliberately runtime-agnostic. Two adapters ship: `LocalRuntime` (in-process, dev/test) and `TemporalRuntime` (production). This document specifies the Temporal mapping.

## Why Temporal

Recipes are durable, multi-step workflows with retry, cancellation, and (sometimes) human-in-the-loop steps. Temporal's primitives map directly:

| Recipe concern | Temporal primitive |
|----------------|--------------------|
| Workflow orchestration | Workflow |
| Long-running source extract | Activity (heartbeated) |
| Compute step | Activity |
| Persist step | Activity |
| Retries on transient failure | Activity retry policy |
| Cancellation | Workflow cancellation |
| Pause / resume / approve | Signals |
| Status visibility | Query handlers |
| Crash resilience | Event-sourced replay |

The infra cost is real (a Temporal cluster). For users who can't run one, `LocalRuntime` keeps everything in-process. Migration between the two requires no recipe changes — recipes are pure logic.

## Workflow per recipe

Each recipe maps to one Temporal workflow type. Workflow type name is `Recipe.name`; workflow version is `Recipe.version`. Submission targets a specific (name, version) pair.

```python
from temporalio import workflow, activity
from temporalio.common import RetryPolicy
from datetime import timedelta

@workflow.defn(name="variance_analysis", sandboxed=True)
class VarianceAnalysisWorkflow:
    @workflow.run
    async def run(self, request: SubmitRequest) -> ResultRef:
        # 1. Validate
        await workflow.execute_activity(
            validate_recipe,
            args=[request.recipe_name, request.params],
            start_to_close_timeout=timedelta(seconds=10),
        )

        # 2. Extract — one activity per source query, in parallel
        ctx = self._build_job_context(request)
        extract_results = await workflow.execute_activity(
            run_extract,
            args=[request.recipe_name, request.params, ctx],
            start_to_close_timeout=timedelta(minutes=20),
            heartbeat_timeout=timedelta(seconds=30),
            retry_policy=RetryPolicy(
                initial_interval=timedelta(seconds=5),
                maximum_attempts=3,
                non_retryable_error_types=[
                    "BudgetExceededError",
                    "SourceSyntaxError",
                    "AuthenticationError",
                ],
            ),
        )

        # 3. Compute
        compute_result = await workflow.execute_activity(
            run_compute,
            args=[request.recipe_name, request.params, ctx, extract_results],
            start_to_close_timeout=timedelta(minutes=60),
            heartbeat_timeout=timedelta(seconds=30),
            retry_policy=RetryPolicy(maximum_attempts=2),
        )

        # 4. Persist
        ref = await workflow.execute_activity(
            run_persist,
            args=[request.recipe_name, request.params, ctx, compute_result],
            start_to_close_timeout=timedelta(minutes=10),
            retry_policy=RetryPolicy(maximum_attempts=5),
        )
        return ref

    @workflow.signal
    async def cancel_request(self):
        # Sets a flag the activities heartbeat against
        ...

    @workflow.query
    def status(self) -> WorkflowStatus:
        ...
```

## Activities

Four activity types, registered by the runtime for every recipe:

### `validate_recipe(recipe_name, params)`
Calls `recipe.validate(params)`. Fast, deterministic, no I/O. Failures are non-retryable.

### `run_extract(recipe_name, params, ctx)`
Calls `recipe.extract(...)`. Returns serialized references to the extracted data — *not the data itself*. See "Where does the data live" below.

### `run_compute(recipe_name, params, ctx, extract_results)`
Sets up DuckDB (memory_limit, threads, scratch_dir from ctx), registers the extracted streams as DuckDB tables, registers the analytical UDF library, calls `recipe.compute(...)`. Returns a `ComputeResult` reference.

### `run_persist(recipe_name, params, ctx, compute_result)`
Calls `recipe.persist(...)` with the configured `ResultStore`. Returns the final `ResultRef`.

## Where does the data live? (The most important design choice)

Temporal activities pass arguments through Temporal's history store. The history is **not** for bulk data — payloads are typically capped at a few MB. We can't pass `pa.RecordBatchReader` or whole tables between activities.

### The pattern

Each activity returns a *handle* to data sitting outside Temporal. The next activity resolves the handle.

```python
@dataclass
class ExtractedDataset:
    storage_uri: str            # s3://bucket/jobs/{job_id}/extract/{name}.parquet
    schema_json: str
    row_count_estimate: int

@dataclass
class ExtractResults:
    datasets: dict[str, ExtractedDataset]
    bytes_total: int
    duration_ms: int
```

`run_extract` streams from `nexcraft` into Parquet on object storage in chunks. Returns `ExtractedDataset` handles. `run_compute` reads them via DuckDB's `read_parquet()` (which supports remote URIs natively).

### Why this matters

- Activities are bounded in payload size — happy Temporal cluster.
- Crash-resilience comes for free: if `run_compute` crashes, retry reads the same Parquet from object storage. Extract doesn't re-run.
- Extracted data is durable and inspectable for debugging — operators can read the staging Parquet directly.
- DuckDB reads Parquet from S3/GCS/Azure efficiently; pushdown into Parquet row groups still works.

### Lifecycle

Staging Parquet under `s3://bucket/jobs/{job_id}/extract/` is deleted on success after persist completes. On failure, kept for the configurable retention window (default 7 days) for post-mortem.

## Heartbeating

Long activities — anything that can run more than 30s — heartbeat. Heartbeats:

- Tell Temporal the activity is alive (otherwise it's considered failed and retried).
- Carry progress information surfaced to operators via the Temporal UI.
- Are the cancellation channel: when the workflow is cancelled, the activity sees a `CancelledError` on the next heartbeat.

```python
@activity.defn
async def run_extract(recipe_name, params, ctx) -> ExtractResults:
    recipe = recipe_registry[recipe_name]
    fedsql = activity.client_factory().fedsql_client()
    streams = await recipe.extract(params, ctx, fedsql)

    results = {}
    for name, reader in streams.items():
        path = f"s3://{BUCKET}/jobs/{ctx.job_id}/extract/{name}.parquet"
        rows = 0
        bytes_ = 0
        async for batch in reader:
            await write_parquet_batch(path, batch)
            rows += batch.num_rows
            bytes_ += batch.nbytes
            activity.heartbeat({
                "phase": "extract",
                "stream": name,
                "rows": rows,
                "bytes": bytes_,
            })
            if activity.is_cancelled():
                ctx.cancel.set()    # propagate to nexcraft stream
                raise activity.CancelledError()
        results[name] = ExtractedDataset(
            storage_uri=path, schema_json=reader.schema.to_string(), row_count_estimate=rows,
        )

    return ExtractResults(datasets=results, ...)
```

The activity sets `ctx.cancel` on Temporal cancellation. The `nexcraft` stream sees this and tears down with source-side cancel. Round-trip cancellation works end-to-end.

## Retry policies

Per-activity, with non-retryable error allowlists:

| Activity | Max attempts | Non-retryable errors |
|----------|--------------|----------------------|
| validate | 1            | All `ValueError` |
| extract  | 3            | `BudgetExceededError`, `SourceSyntaxError`, `AuthenticationError`, `ConfigurationError` |
| compute  | 2            | `BudgetExceededError`, recipe-specific value errors |
| persist  | 5            | `ConfigurationError` |

Connection errors and timeouts are retryable. Anything that won't change on retry isn't.

## Signals and queries

### Cancel signal

```python
await client.signal_workflow(
    workflow_id=f"job-{job_id}",
    signal="cancel_request",
)
```

Workflow handler sets a flag; activities heartbeat against the flag and bail cleanly.

### Status query

```python
status = await client.query_workflow(workflow_id=f"job-{job_id}", query="status")
# Returns: {phase, started_at, current_activity, progress: {rows, bytes}, deadline}
```

Used by the optional HTTP API to power `GET /v1/jobs/{id}`.

### Pause/resume (recipe-opt-in)

For recipes that support human-in-the-loop, the runtime supports an `approve` signal. The workflow blocks on `await workflow.wait_condition(lambda: self._approved)` between specific phases. Most recipes don't need this; the pattern is documented for those that do.

## Worker layout

A worker process registers all recipe workflows + the four activities. Workers are stateless and horizontally scalable. Recommended pattern:

- One worker pool for `validate` + `persist` activities (low resource).
- One worker pool for `extract` activities (memory-bounded by extract budgets).
- One worker pool for `compute` activities (CPU + memory heavy; sized to memory_budget).

Different task queues per pool so resource sizing matches workload.

```python
worker_extract = Worker(
    client, task_queue="nexcraft-extract",
    workflows=[...],          # all recipe workflows
    activities=[run_extract, validate_recipe],
    max_concurrent_activities=8,
)
worker_compute = Worker(
    client, task_queue="nexcraft-compute",
    activities=[run_compute],
    max_concurrent_activities=2,   # memory-bound
)
```

The workflow uses `task_queue=...` per activity to route to the right pool.

## LocalRuntime (for dev/test)

Same recipe code, no Temporal. In-process orchestration:

```python
class LocalRuntime:
    async def submit(self, recipe_name, params, ctx) -> ResultRef:
        recipe = self._registry[recipe_name]
        recipe.validate(params)
        streams = await recipe.extract(params, ctx, self._fedsql)
        # Skip Parquet staging; pass streams in-memory
        con = self._setup_duckdb(ctx)
        for name, reader in streams.items():
            con.register(name, reader)
        register_analytical_udfs(con)
        ctx._duckdb = con
        compute_result = await recipe.compute(streams, params, ctx)
        return await recipe.persist(compute_result, params, ctx, self._store)
```

For development: run a recipe in 50ms instead of waiting for Temporal. For tests: deterministic, no infra. For production: not used.
