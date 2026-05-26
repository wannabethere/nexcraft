# Jobs 00 — Overview

`nexcraft-jobs` is the optional companion package that adds an analytical jobs framework on top of `nexcraft`. It exists because federated SQL alone doesn't cover the full analytical workflow:

- Variance analysis across actuals and forecasts.
- Trend detection and decomposition (STL, changepoints).
- Cohort analysis with retention curves.
- What-if simulation over scenario parameters.
- Anomaly detection on time series.

These are *recipes* — multi-step compute pipelines that pull from sources, transform, and produce structured insights. They have a different latency budget, different memory budget, different failure model, and a different API shape than live federated SQL.

## Two service paths, deliberately separate

```
┌──────────────────────────────────────────────────────────────────┐
│  Path 1: Live federated SQL  (nexcraft)                          │
│    Sub-second to seconds latency.                                │
│    Synchronous request/response.                                 │
│    Single source per query, full pushdown.                       │
│    Caller: agent, BI tool, app, notebook.                        │
└──────────────────────────────────────────────────────────────────┘

┌──────────────────────────────────────────────────────────────────┐
│  Path 2: Analytical jobs     (nexcraft-jobs)                     │
│    Seconds to hours latency.                                     │
│    Asynchronous; Temporal workflow.                              │
│    Multi-source extract → DuckDB compute → Parquet result.       │
│    Caller: scheduler, agent, user-triggered submit.              │
└──────────────────────────────────────────────────────────────────┘
```

Path 2 *uses* Path 1 — recipes call `FedSQLClient.execute` for their extract phase. There is no separate batch federation API. Same federation primitive, different caller, different budgets.

## Why Temporal

Recipes have non-trivial control flow: multi-step extract, retries on transient source failures, checkpointing so a 30-minute compute doesn't restart from scratch on a worker crash, signals for cancellation, and human-in-the-loop steps for approve/reject patterns.

Options considered:

| Runtime         | Pros                                            | Cons                                          |
|-----------------|-------------------------------------------------|-----------------------------------------------|
| Celery          | Ubiquitous, simple                              | No native checkpointing; retry semantics weak |
| Prefect         | Pythonic, modern                                | Cloud-first; OSS has fewer guarantees         |
| Dagster         | Asset-oriented, great for DAGs                  | Heavier than needed; opinionated about layout |
| **Temporal**    | Durable execution; replay; signals; activities  | Extra infra (Temporal server)                 |

Temporal wins because durable execution maps directly to the recipe model: each step is an activity, the workflow orchestrates them, retries are declarative, state is automatic. The infra cost (a Temporal cluster) is real but reasonable; for OSS adopters who don't want it, the `LocalRuntime` runs everything in-process for dev and small workloads.

[ADR 003](../decisions/003-temporal-for-jobs.md) records this decision in detail.

## The four phases

Every recipe goes through the same four phases. This is the contract.

```
┌──────────┐     ┌──────────┐     ┌──────────┐     ┌──────────┐
│ Extract  │────▶│  Stage   │────▶│ Compute  │────▶│ Persist  │
└──────────┘     └──────────┘     └──────────┘     └──────────┘
   uses             registers          DuckDB +         Parquet to
   nexcraft         arrow              UDFs +           object store +
   client           streams in         scipy            metadata row
                    DuckDB
```

### Extract
One or more federated SQL queries via `nexcraft.FedSQLClient`. Returns `pa.RecordBatchReader` per logical input — lazy streams, not materialized tables. Aggressive `max_rows` / `max_bytes` budgets fail fast if filters are too loose.

### Stage
Register Arrow streams as DuckDB tables. Zero-copy. Done by the runtime, not the recipe author — recipes get pre-registered tables.

### Compute
The recipe's actual work. Pure SQL where possible (variance decomposition, window functions, cohort joins). Python UDFs over Arrow for statistical methods that aren't expressible in SQL (STL, ARIMA, Prophet, changepoints). DuckDB does the orchestration; Python UDFs do the math; everything stays vectorized.

### Persist
Result `pa.Table` written to Parquet on object storage. A metadata row is written to a Postgres table (`job_runs`) capturing job_id, recipe, params, result paths, status, timing. Returns a `ResultRef` callers can resolve.

## What `nexcraft-jobs` provides

- The `Recipe` protocol and the four-phase contract.
- Two runtime adapters: `LocalRuntime` (in-process, for dev) and `TemporalRuntime` (production).
- A DuckDB compute layer with budgets, spilling, and a registered analytical UDF library.
- Result storage primitives (Parquet + metadata).
- Five reference recipes (variance, trend, cohort, anomaly, what-if).
- An optional HTTP API for job submission and status polling.
- A CLI for running recipes locally.

## What `nexcraft-jobs` does *not* provide

- A scheduler. Use Airflow, Temporal Cron, or a cron job that calls the submit API.
- A UI. Submission and status are API + CLI; downstream apps build their own UI.
- A recipe marketplace. Recipes are Python classes; users distribute them as their own packages.
- Auth. The submission API takes a JWT; auth backend is a caller concern.
- Multi-tenant isolation beyond what Temporal namespaces give you.

## Reading order for `nexcraft-jobs`

1. [`01-recipes.md`](01-recipes.md) — the recipe pattern in detail.
2. [`02-temporal.md`](02-temporal.md) — workflow / activity layout, durable execution, signals.
3. [`03-duckdb-udfs.md`](03-duckdb-udfs.md) — compute layer and analytical UDF library.
4. [`04-storage.md`](04-storage.md) — result persistence and metadata.
