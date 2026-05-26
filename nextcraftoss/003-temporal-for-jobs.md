# ADR 003 — Temporal as the Jobs Runtime

**Status:** Accepted
**Date:** 2026-05

## Context

`nexcraft-jobs` runs analytical recipes: multi-step pipelines that pull from sources, compute, and persist. Recipes have non-trivial requirements:

- Multiple steps with long-running activities (extract phase can take 10+ minutes for a TB extract).
- Retry policies that distinguish transient (connection drop) from terminal (syntax error) failures.
- Crash resilience — a worker dying mid-compute shouldn't restart from extract.
- Cancellation that propagates to in-flight source queries.
- Status visibility for the calling agent or app.
- Optional human-in-the-loop steps (approve/reject after extract).

We need a runtime for this.

## Decision

**Temporal** is the production runtime. A `LocalRuntime` ships alongside for development and testing.

## Consequences

### Why Temporal

Temporal's primitives map directly to the recipe model:

| Recipe concern | Temporal primitive |
|---|---|
| Multi-step orchestration | Workflow |
| Long-running source extract | Activity (heartbeated) |
| Retries with allowlists | Activity retry policy + non-retryable error types |
| Cancellation propagation | Workflow cancellation → activity heartbeat detects → propagate |
| Crash resilience | Event-sourced replay |
| Status visibility | Query handlers |
| Pause/resume | Signals + `wait_condition` |

Equally important: Temporal is **not** opinionated about workflow structure. We define the recipe shape; Temporal executes it. Compare to Dagster (asset-oriented, opinionated DAG model) or Airflow (DAG-of-tasks, opinionated scheduling).

### What we accept

- **Operational dependency.** A Temporal cluster (or Temporal Cloud account) is required for production. This is real infrastructure cost and complexity.
- **Workflow constraints.** Workflows run in a deterministic sandbox — no random IDs, no `datetime.now()` in workflow code, no I/O. All non-determinism happens in activities. Recipe authors don't write workflows; the runtime generates them. So this constraint is invisible to recipe authors.
- **Payload limits.** Temporal history events have a soft cap (a few MB). We can't pass `pa.Table` objects between activities. The fix: stage extracted data as Parquet on object storage and pass URIs through the workflow. Documented and standard pattern.

### Why not Celery

- No native checkpointing. A worker crash mid-job loses progress.
- Retry semantics are basic. Distinguishing "retry the connection error, fail fast on the syntax error" requires custom retry classes and isn't first-class.
- No cancellation primitive. `task.revoke()` is best-effort.
- No status query model. Building "what's this job doing?" means storing state ourselves.

Celery is fine for fire-and-forget tasks. Recipes are not those.

### Why not Prefect

- Strong cloud-first emphasis; OSS Prefect has less guarantee around long-term direction than Temporal.
- Prefect 2/3's execution model has churned more than Temporal's stable Workflow/Activity model.
- Workflow versioning story is weaker.

### Why not Dagster

- Asset-oriented model: Dagster wants to know about your data assets and dependencies. Recipes are imperative pipelines; the asset model is a poor fit.
- Heavier than needed; bundles a UI, scheduler, and asset graph that compete with the rest of our stack.

### Why not "just use asyncio"

This is what `LocalRuntime` is for, and it's correct for development. For production:

- A worker crash loses jobs.
- Retries are caller-implemented.
- Cancellation requires custom plumbing.
- No status visibility beyond logs.

We'd reinvent half of Temporal, badly.

## What this means for the project

### Two runtimes, same recipes

Recipe code is the same regardless of runtime. The runtime adapter is what differs:

- `LocalRuntime` — async in-process, in-memory streams, no Parquet staging. For dev and tests.
- `TemporalRuntime` — workflows + activities, Parquet staging, retries, durable.

Migration from local to Temporal requires no recipe changes. This is a hard requirement on the abstraction.

### Operators who don't want Temporal

For users who genuinely can't run Temporal infrastructure:

- `LocalRuntime` is production-acceptable for low-volume / non-critical workloads. Document this honestly.
- A future `CeleryRuntime` adapter is plausible if there's pull. Not v0.1.

### What we ship in v0.1

- `LocalRuntime` — fully featured, in-process.
- `TemporalRuntime` — feature-complete: workflows, activities, retries, cancellation, signals, queries.
- Documented patterns for: deploying Temporal workers, sizing worker pools, configuring task queues.

## Operational guidance

`nexcraft-jobs` doesn't ship a Temporal server. Production deployments use either:

- Self-hosted Temporal cluster (open source). Substantial but bounded ops investment.
- Temporal Cloud (managed). Pay-per-use; trivial to start.

We document both. Neither is endorsed; both are reasonable.

## When this should be revisited

- If Temporal Cloud's pricing or licensing changes materially.
- If a clearly better OSS-friendly orchestrator emerges (Restate is in this space and worth watching).
- If the project's user base skews so heavily toward "no Temporal" that the LocalRuntime + something-simpler covers 95% of needs.
