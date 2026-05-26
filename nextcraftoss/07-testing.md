# 07 — Testing Strategy

The thing that determines whether the OSS project survives. Three layers.

## 1. Protocol conformance suite

The single highest-leverage investment in the project. Without it, every third-party executor is subtly broken in a different way.

A pytest plugin shipped under `nexcraft.testing.conformance`. Anyone — first or third party — can validate their `SourceExecutor` against the protocol contract:

```bash
pytest --pyargs nexcraft.testing.conformance \
       --executor=mypackage.executors.MyExecutor \
       --connection-provider=mypackage.providers.MyConnectionProvider \
       --catalog-fixture=tests/fixtures/catalog.yaml
```

### What the suite tests

| Category | Tests |
|----------|-------|
| **Basic execution** | Simple SELECT returns expected schema. Empty result returns valid empty stream. Multi-batch result preserves order. |
| **Schema correctness** | `describe()` matches `execute()` schema. All Arrow types declared in the suite are correctly typed. |
| **Streaming** | Stream yields multiple batches for large results. Backpressure works (slow consumer doesn't OOM producer). Schema is stable across batches. |
| **Cancellation** | `task.cancel()` propagates within 1s. `ctx.cancel.set()` propagates within 1s. Source-side query is actually cancelled (verified by querying source's process list). |
| **Deadlines** | Deadline expiry raises `TimeoutError`. Source-side query is cancelled on deadline. |
| **Budgets** | `max_rows` exceeded raises `BudgetExceededError(budget_kind="rows")`. `max_bytes` ditto. Streams stop on budget violation. |
| **Errors** | Syntax errors raise `SourceSyntaxError`. Auth failures raise `AuthenticationError`. Connection drops raise `ConnectionError`. Driver exceptions never escape. |
| **Idempotency** | `describe()` is side-effect free. `describe()` can be called multiple times for the same SQL. |
| **Resource cleanup** | After error, no connections leak. After cancellation, no connections leak. After normal completion, no connections leak. |

### Conformance fixtures

The suite needs a known-state source. Two fixture strategies:

- **Container-based** — the suite spins up a Postgres / MinIO+Iceberg container, loads a known dataset, runs tests. Required for first-party executors in CI.
- **External-source** — the suite is given connection details to a pre-loaded source. Required for sources that can't be containerized (Snowflake, BigQuery). Gated behind credentials.

Conformance dataset is small (~10MB) but covers: all primitive types, nullable columns, timestamps with/without TZ, arrays, JSON/JSONB-equivalents where supported, large enough to span multiple batches at default size.

### Why this matters for OSS adoption

Third-party executors are how the project scales. If writing one is "implement a protocol and figure out the corner cases yourself," contributors give up. If it's "implement a protocol, run the conformance suite, fix what fails," contributors succeed. The conformance suite is the executor SDK.

## 2. Containerized integration tests

Per-executor integration tests against real sources. Run on every PR via GitHub Actions.

### Matrix

| Executor   | CI mechanism                                                        |
|------------|---------------------------------------------------------------------|
| Postgres   | `services: postgres` in workflow                                    |
| Snowflake  | gated job using a CI account; runs on `main` and labeled PRs        |
| BigQuery   | same as Snowflake                                                   |
| Iceberg    | MinIO + REST catalog (`tabular-io/iceberg-rest`) services           |
| Delta      | MinIO services                                                      |

Each test suite runs:

1. Conformance suite (above).
2. Source-specific tests (e.g., Postgres `pg_cancel_backend` actually cancels; Iceberg pushdown is observable in scan stats).
3. End-to-end with the public client and router.

### Cost discipline

Snowflake/BigQuery accounts cost money for queries. Tests are designed to use:

- Tiny datasets (kilobytes).
- Result-set caching where the source supports it.
- A budget cap per CI run.

Document this in the CONTRIBUTING guide so contributors don't burn the project's CI credits.

## 3. Unit tests

Standard. Each module has unit tests for its non-I/O logic:

- `streaming` — `CancellableArrowStream` with a fake producer. Cancellation, deadlines, budgets, backpressure.
- `router` — dispatch logic with mocked executors and catalog.
- `errors` — translation of common driver exceptions to `nexcraft.errors`.
- `core` — `QueryContext` immutability, `replace()` semantics, validation.

Coverage target: 90%+ for `nexcraft.core` and `nexcraft.streaming`. 80% overall (executors are integration-tested rather than unit-tested).

## Public benchmarks

Run nightly. Published to a `gh-pages` site. Reproducible by anyone.

### Benchmark scenarios

- **TPC-H subset (Q1, Q3, Q5, Q10) on Postgres** — full-pushdown latency, time-to-first-batch.
- **TPC-H subset on Snowflake** — same queries; partition fetching effect on throughput.
- **TPC-H subset on Iceberg** — pushdown effectiveness (files-pruned ratio reported).
- **Streaming throughput** — stream a 10M-row table from each source; report rows/sec, MB/sec, memory high-water.
- **Cancellation latency** — start a long query, cancel after 100ms; measure time until source-side query is gone.

Report:
- p50, p95, p99 latencies.
- Time-to-first-batch (the metric users actually care about).
- Memory high-water mark.
- Whether `nexcraft` adds overhead vs raw driver use (target: <5% on streaming, <50ms on time-to-first-batch).

Versioned per release. Regressions are blocking issues.

### Why public benchmarks matter

Adoption-driver in the analytics OSS world. Users want to know what they're getting before they install. Published, reproducible, regenerated nightly = trust.

## CI workflow shape

```yaml
# .github/workflows/ci.yml
on: [pull_request, push]

jobs:
  lint-and-type:
    runs-on: ubuntu-latest
    steps:
      - ruff check .
      - ruff format --check .
      - pyright

  unit-tests:
    strategy:
      matrix:
        python: ["3.11", "3.12", "3.13"]
        os: [ubuntu-latest, macos-latest]
    steps:
      - pytest tests/unit -v --cov=nexcraft

  integration-postgres:
    services:
      postgres:
        image: postgres:16
    steps:
      - pytest tests/integration/postgres
      - pytest --pyargs nexcraft.testing.conformance --executor=nexcraft.executors.postgres.PostgresExecutor

  integration-iceberg:
    services:
      minio: ...
      iceberg-rest: ...
    steps:
      - pytest tests/integration/iceberg
      - pytest --pyargs nexcraft.testing.conformance --executor=nexcraft.executors.iceberg.IcebergExecutor

  integration-snowflake:
    if: github.event_name == 'push' || contains(github.event.pull_request.labels.*.name, 'snowflake-ci')
    steps:
      - pytest tests/integration/snowflake
        env:
          SNOWFLAKE_ACCOUNT: ${{ secrets.SNOWFLAKE_ACCOUNT }}
          ...
```

Nightly benchmark workflow is separate (`benchmarks.yml`), runs on schedule, posts results to `gh-pages`.
